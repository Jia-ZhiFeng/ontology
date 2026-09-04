import asyncio
import json
import re
import threading
import weakref
from contextlib import asynccontextmanager
from urllib.parse import quote,urlsplit,urlunsplit

import httpx

from ..core.config import (
    LLM_ALLOWED_BASE_URLS,
    LLM_ALLOW_INSECURE_LOCAL,
    LLM_REQUEST_TIMEOUT_SECONDS,
)
from ..core.security import validate_llm_base_url


PROVIDER_PRESETS={
    "openai_compatible":{
        "label":"OpenAI Compatible",
        "default_base_url":"https://api.openai.com/v1",
        "base_url_editable":True,
        "requires_admin_allowlist":True,
    },
    "deepseek":{
        "label":"DeepSeek",
        "default_base_url":"https://api.deepseek.com",
        "base_url_editable":False,
        "requires_admin_allowlist":False,
    },
    "aliyun_bailian":{
        "label":"阿里云百炼",
        "default_base_url":"https://dashscope.aliyuncs.com/compatible-mode/v1",
        "base_url_editable":True,
        "requires_admin_allowlist":False,
    },
    "azure_openai":{
        "label":"Azure OpenAI",
        "default_base_url":"",
        "base_url_editable":True,
        "requires_admin_allowlist":True,
    },
    "custom":{
        "label":"Custom OpenAI Compatible",
        "default_base_url":"",
        "base_url_editable":True,
        "requires_admin_allowlist":True,
    },
}

_PROVIDER_ALIASES={
    "openai":"openai_compatible",
    "openai-compatible":"openai_compatible",
    "openai compatible":"openai_compatible",
    "deepseek":"deepseek",
    "aliyun":"aliyun_bailian",
    "bailian":"aliyun_bailian",
    "dashscope":"aliyun_bailian",
    "aliyun-bailian":"aliyun_bailian",
    "azure":"azure_openai",
    "azure-openai":"azure_openai",
    "custom":"custom",
}
_AZURE_API_VERSION=re.compile(r"^\d{4}-\d{2}-\d{2}(?:-preview)?$")
_BAILIAN_WORKSPACE_HOST=re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\."
    r"(?:cn-beijing|ap-southeast-1|ap-northeast-1|eu-central-1|us-east-1)"
    r"\.maas\.aliyuncs\.com$"
)
LLM_DEFAULT_MAX_OUTPUT_TOKENS=4096
LLM_MAX_OUTPUT_TOKENS=8192
LLM_HTTP_MAX_CONNECTIONS=50
LLM_HTTP_MAX_KEEPALIVE_CONNECTIONS=20
LLM_HTTP_KEEPALIVE_EXPIRY_SECONDS=30.0


class LLMTransientError(RuntimeError):
    """A timeout, rate limit, or server error that is safe to retry once."""


class _LoopHTTPClientState:
    """One shared HTTP client and its in-flight leases for an event loop."""

    def __init__(self):
        self.condition=asyncio.Condition()
        self.client=None
        self.active_requests=0
        self.closing=False
        self.retired=False


class _SharedHTTPClientPool:
    """Reuse bounded httpx pools without sharing transports across event loops."""

    def __init__(self):
        self._states=weakref.WeakKeyDictionary()
        self._states_lock=threading.Lock()

    def _state(self,loop,*,create:bool):
        with self._states_lock:
            state=self._states.get(loop)
            if state is None and create:
                state=_LoopHTTPClientState()
                self._states[loop]=state
            return state

    @staticmethod
    def _new_client():
        return httpx.AsyncClient(
            timeout=LLM_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=LLM_HTTP_MAX_CONNECTIONS,
                max_keepalive_connections=LLM_HTTP_MAX_KEEPALIVE_CONNECTIONS,
                keepalive_expiry=LLM_HTTP_KEEPALIVE_EXPIRY_SECONDS,
            ),
        )

    async def _acquire(self):
        loop=asyncio.get_running_loop()
        while True:
            state=self._state(loop,create=True)
            async with state.condition:
                while state.closing and not state.retired:
                    await state.condition.wait()
                if state.retired:
                    continue
                if state.client is None:
                    state.client=self._new_client()
                state.active_requests+=1
                return state,state.client

    @staticmethod
    async def _release(state):
        async with state.condition:
            state.active_requests=max(0,state.active_requests-1)
            if state.active_requests==0:
                state.condition.notify_all()

    @asynccontextmanager
    async def lease(self):
        state,client=await self._acquire()
        try:
            yield client
        finally:
            await self._release(state)

    @staticmethod
    async def _close_client(client):
        # Application shutdown cancellation must not strand a half-closed
        # connection pool. Finish the close, then preserve cancellation.
        close_task=asyncio.create_task(client.aclose())
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            await close_task
            raise

    async def aclose_current_loop(self):
        """Drain in-flight calls, close this loop's pool, and allow recreation."""
        loop=asyncio.get_running_loop()
        state=self._state(loop,create=False)
        if state is None:
            return
        async with state.condition:
            while state.closing and not state.retired:
                await state.condition.wait()
            if state.retired:
                return
            state.closing=True
            try:
                await state.condition.wait_for(
                    lambda:state.active_requests==0
                )
            except BaseException:
                state.closing=False
                state.condition.notify_all()
                raise
            client=state.client
        try:
            if client is not None:
                await self._close_client(client)
        finally:
            async with state.condition:
                state.client=None
                state.retired=True
                state.closing=False
                with self._states_lock:
                    if self._states.get(loop) is state:
                        del self._states[loop]
                state.condition.notify_all()


_SHARED_HTTP_CLIENT_POOL=_SharedHTTPClientPool()


async def aclose_llm_http_clients():
    """Close the shared LLM pool from the owning application lifespan.

    Call this after the application stops accepting new LLM work. It waits for
    in-flight requests and is safe to call more than once.
    """
    await _SHARED_HTTP_CLIENT_POOL.aclose_current_loop()


def normalize_provider(provider:str|None)->str:
    value=(provider or "openai_compatible").strip().lower().replace("_","-")
    normalized=_PROVIDER_ALIASES.get(value,value.replace("-","_"))
    if normalized not in PROVIDER_PRESETS:
        raise ValueError(f"不支持的 LLM 提供方：{provider}")
    return normalized


def provider_catalog()->list[dict]:
    return [
        {"id":provider,**preset}
        for provider,preset in PROVIDER_PRESETS.items()
    ]


def normalize_llm_endpoint(provider:str|None,base_url:str|None)->tuple[str,str]:
    normalized_provider=normalize_provider(provider)
    preset=PROVIDER_PRESETS[normalized_provider]
    requested=(base_url or "").strip() or preset["default_base_url"]
    if not requested:
        raise ValueError("请填写 LLM Base URL")
    if normalized_provider=="aliyun_bailian":
        return normalized_provider,_validate_bailian_endpoint(requested)
    # DeepSeek uses a fixed vendor-owned HTTPS endpoint. Other providers must
    # exactly match an administrator-controlled allowlist entry.
    if normalized_provider=="deepseek":
        allowed=["https://api.deepseek.com","https://api.deepseek.com/v1"]
    else:
        allowed=(
            [preset["default_base_url"]]
            if not preset["requires_admin_allowlist"]
            else LLM_ALLOWED_BASE_URLS
        )
    canonical=validate_llm_base_url(
        requested,allowed,LLM_ALLOW_INSECURE_LOCAL
    )
    return normalized_provider,canonical


def _validate_bailian_endpoint(base_url:str)->str:
    value=(base_url or "").strip().rstrip("/")
    parsed=urlsplit(value)
    host=(parsed.hostname or "").lower()
    try:
        port=parsed.port
    except ValueError as exc:
        raise ValueError("阿里云百炼地址端口不正确") from exc
    allowed_host=(
        host in {
            "dashscope.aliyuncs.com","dashscope-intl.aliyuncs.com",
            "dashscope-us.aliyuncs.com",
        }
        or bool(_BAILIAN_WORKSPACE_HOST.fullmatch(host))
    )
    if (
        parsed.scheme.lower()!="https"
        or not allowed_host
        or port not in {None,443}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/")!="/compatible-mode/v1"
    ):
        raise ValueError(
            "阿里云百炼地址必须是官方 Compatible HTTPS 地址或受支持的 Workspace 地址"
        )
    return urlunsplit(("https",host,parsed.path.rstrip("/"),"",""))


class LLMClient:
    def __init__(
        self,api_key,base_url,model,temperature=0,
        provider="openai_compatible",api_version=None,
    ):
        self.api_key=str(api_key or "")
        self.provider,self.base_url=normalize_llm_endpoint(provider,base_url)
        self.model=str(model or "").strip()
        self.temperature=float(temperature)
        self.api_version=(api_version or "2024-10-21").strip()
        if not 0<=self.temperature<=2:
            raise ValueError("LLM temperature 必须在 0 到 2 之间")
        if self.provider=="azure_openai" and not _AZURE_API_VERSION.fullmatch(
            self.api_version
        ):
            raise ValueError("Azure API Version 格式不正确")

    def _request(self)->tuple[str,dict]:
        if self.provider=="azure_openai":
            deployment=quote(self.model,safe="")
            url=(
                f"{self.base_url}/openai/deployments/{deployment}"
                f"/chat/completions?api-version={self.api_version}"
            )
            return url,{"api-key":self.api_key,"Content-Type":"application/json"}
        return (
            self.base_url+"/chat/completions",
            {"Authorization":f"Bearer {self.api_key}","Content-Type":"application/json"},
        )

    @staticmethod
    def _temperature(value)->float:
        if isinstance(value,bool):
            raise ValueError("LLM temperature 必须是 0 到 2 之间的数值")
        try:
            normalized=float(value)
        except (TypeError,ValueError) as exc:
            raise ValueError("LLM temperature 必须是 0 到 2 之间的数值") from exc
        if not 0<=normalized<=2:
            raise ValueError("LLM temperature 必须在 0 到 2 之间")
        return normalized

    @staticmethod
    def _max_tokens(value)->int:
        if isinstance(value,bool) or not isinstance(value,int):
            raise ValueError("LLM max_tokens 必须是整数")
        if not 1<=value<=LLM_MAX_OUTPUT_TOKENS:
            raise ValueError(
                f"LLM max_tokens 必须在 1 到 {LLM_MAX_OUTPUT_TOKENS} 之间"
            )
        return value

    async def chat_json(
        self,system,user,*,temperature=None,max_tokens=None
    ):
        if not self.api_key.strip():
            raise ValueError("请填写 LLM API Key")
        if not self.base_url.strip():
            raise ValueError("请填写 LLM Base URL")
        if not self.model.strip():
            raise ValueError("请填写 LLM 模型名称")
        request_temperature=self._temperature(
            self.temperature if temperature is None else temperature
        )
        request_max_tokens=self._max_tokens(
            LLM_DEFAULT_MAX_OUTPUT_TOKENS if max_tokens is None else max_tokens
        )
        url,headers=self._request()
        payload={
            "model":self.model,
            "temperature":request_temperature,
            "max_tokens":request_max_tokens,
            "messages":[
                {"role":"system","content":system},
                {"role":"user","content":user},
            ],
            "response_format":{"type":"json_object"},
        }
        # Azure identifies the deployment in the URL and rejects a model field
        # on some API versions.
        if self.provider=="azure_openai":
            payload.pop("model",None)
        try:
            async with _SHARED_HTTP_CLIENT_POOL.lease() as client:
                response=await client.post(url,headers=headers,json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTransientError("LLM 请求超时，请检查服务地址或网络连接") from exc
        except httpx.RequestError as exc:
            raise RuntimeError("无法连接 LLM 服务，请检查地址和网络策略") from exc
        if not response.is_success:
            request_id=(
                response.headers.get("x-request-id")
                or response.headers.get("request-id")
            )
            if request_id and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}",request_id):
                request_id=None
            suffix=f"，请求 ID：{request_id}" if request_id else ""
            error=(
                f"LLM 返回 HTTP {response.status_code}"
                f"（{response.reason_phrase or '请求失败'}）{suffix}"
            )
            if response.status_code==429 or response.status_code>=500:
                raise LLMTransientError(error)
            raise RuntimeError(error)
        try:
            data=response.json()
            content=data["choices"][0]["message"]["content"]
        except (ValueError,KeyError,IndexError,TypeError) as exc:
            raise RuntimeError(
                "LLM 返回格式不正确，缺少 choices[0].message.content"
            ) from exc
        if not isinstance(content,str) or not content.strip():
            raise RuntimeError("LLM 返回了空内容")
        return content.strip()

    async def test(self):
        content=await self.chat_json(
            "Return exactly one JSON object and no surrounding text.",
            '{"ping":"pong"}',
        )
        try:
            parsed=json.loads(content)
        except (TypeError,ValueError) as exc:
            raise RuntimeError("LLM 连接成功，但未返回有效 JSON 对象") from exc
        if not isinstance(parsed,dict):
            raise RuntimeError("LLM 连接成功，但结构化响应不是 JSON 对象")
        return content
