import httpx
from ..core.config import LLM_ALLOWED_BASE_URLS,LLM_ALLOW_INSECURE_LOCAL
from ..core.security import validate_llm_base_url

class LLMTransientError(RuntimeError):
    """A timeout, rate limit, or server error that is safe to retry once."""

class LLMClient:
    def __init__(self,api_key,base_url,model,temperature=0):
        self.api_key=api_key
        self.base_url=validate_llm_base_url(base_url,LLM_ALLOWED_BASE_URLS,LLM_ALLOW_INSECURE_LOCAL)
        self.model=model
        self.temperature=temperature
    async def chat_json(self,system,user):
        if not self.api_key.strip(): raise ValueError("请填写 LLM API Key")
        if not self.base_url.strip(): raise ValueError("请填写 LLM Base URL")
        if not self.model.strip(): raise ValueError("请填写 LLM 模型名称")
        h={"Authorization":f"Bearer {self.api_key}","Content-Type":"application/json"}
        p={"model":self.model,"temperature":self.temperature,"messages":[{"role":"system","content":system},{"role":"user","content":user}],"response_format":{"type":"json_object"}}
        try:
            async with httpx.AsyncClient(timeout=240) as c:
                r=await c.post(self.base_url+"/chat/completions",headers=h,json=p)
        except httpx.TimeoutException as exc:
            raise LLMTransientError("LLM 请求超时，请检查服务地址或网络连接") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"无法连接 LLM 服务：{exc!r}") from exc
        if not r.is_success:
            request_id=r.headers.get("x-request-id") or r.headers.get("request-id")
            suffix=f"，请求 ID：{request_id}" if request_id else ""
            error=f"LLM 返回 HTTP {r.status_code}（{r.reason_phrase or '请求失败'}）{suffix}"
            if r.status_code==429 or r.status_code>=500:
                raise LLMTransientError(error)
            raise RuntimeError(error)
        try:
            data=r.json(); content=data["choices"][0]["message"]["content"]
        except (ValueError,KeyError,IndexError,TypeError) as exc:
            raise RuntimeError("LLM 返回格式不正确，缺少 choices[0].message.content") from exc
        if not isinstance(content,str) or not content.strip():
            raise RuntimeError("LLM 返回了空内容")
        return content.strip()
    async def test(self): return await self.chat_json("Return JSON only.",'{"ping":"pong"}')
