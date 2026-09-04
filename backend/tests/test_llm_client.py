import asyncio
import unittest
from unittest.mock import patch

import httpx

from app.llm.client import (
    LLMClient,
    LLM_DEFAULT_MAX_OUTPUT_TOKENS,
    LLM_MAX_OUTPUT_TOKENS,
    LLMTransientError,
    aclose_llm_http_clients,
)


class SuccessfulResponse:
    is_success=True
    status_code=200
    reason_phrase="OK"
    headers={}

    @staticmethod
    def json():
        return {"choices":[{"message":{"content":"{\"ok\":true}"}}]}


class RecordingAsyncClient:
    def __init__(self,*,gate=None,started=None):
        self.gate=gate
        self.started=started
        self.posts=[]
        self.closed=False

    async def post(self,url,**kwargs):
        self.posts.append((url,kwargs))
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        return SuccessfulResponse()

    async def aclose(self):
        self.closed=True


class LLMHTTPClientPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await aclose_llm_http_clients()

    @staticmethod
    def client():
        return LLMClient(
            api_key="test-secret",
            provider="deepseek",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
        )

    async def test_reuses_bounded_client_and_caps_structured_output(self):
        created=[]

        def factory(**kwargs):
            instance=RecordingAsyncClient()
            created.append((instance,kwargs))
            return instance

        with patch("app.llm.client.httpx.AsyncClient",side_effect=factory):
            first=self.client()
            second=self.client()
            await first.chat_json("system one","user one")
            await second.chat_json("system two","user two")

        self.assertEqual(len(created),1)
        shared,options=created[0]
        self.assertEqual(len(shared.posts),2)
        self.assertFalse(options["follow_redirects"])
        self.assertIsInstance(options["limits"],httpx.Limits)
        for _url,request in shared.posts:
            self.assertEqual(
                request["json"]["max_tokens"],
                LLM_DEFAULT_MAX_OUTPUT_TOKENS,
            )
        await aclose_llm_http_clients()
        self.assertTrue(shared.closed)

    async def test_close_waits_for_concurrent_request_and_pool_can_reopen(self):
        gate=asyncio.Event()
        both_started=asyncio.Event()
        created=[]
        start_count=0

        class ConcurrentClient(RecordingAsyncClient):
            async def post(self,url,**kwargs):
                nonlocal start_count
                start_count+=1
                if start_count==2:
                    both_started.set()
                return await super().post(url,**kwargs)

        def factory(**_kwargs):
            instance=(
                ConcurrentClient(gate=gate)
                if not created else RecordingAsyncClient()
            )
            created.append(instance)
            return instance

        with patch("app.llm.client.httpx.AsyncClient",side_effect=factory):
            client=self.client()
            calls=[
                asyncio.create_task(client.chat_json("system","first")),
                asyncio.create_task(client.chat_json("system","second")),
            ]
            await asyncio.wait_for(both_started.wait(),1)
            closing=asyncio.create_task(aclose_llm_http_clients())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            self.assertFalse(created[0].closed)
            during_close=asyncio.create_task(
                client.chat_json("system","during close")
            )
            await asyncio.sleep(0)
            self.assertFalse(during_close.done())
            gate.set()
            await asyncio.gather(*calls)
            await closing
            await during_close
            self.assertTrue(created[0].closed)

            await client.chat_json("system","after close")
            self.assertEqual(len(created),2)
            self.assertEqual(len(created[1].posts),2)

    async def test_per_call_overrides_are_validated_and_sent(self):
        created=[]

        def factory(**_kwargs):
            instance=RecordingAsyncClient()
            created.append(instance)
            return instance

        with patch("app.llm.client.httpx.AsyncClient",side_effect=factory):
            client=self.client()
            await client.chat_json(
                "system","user",temperature=0.35,max_tokens=800
            )
            for invalid in (-0.1,2.1,float("nan"),True,"cold"):
                with self.subTest(temperature=invalid),self.assertRaises(ValueError):
                    await client.chat_json(
                        "system","user",temperature=invalid
                    )
            for invalid in (0,LLM_MAX_OUTPUT_TOKENS+1,True,1.5,"800"):
                with self.subTest(max_tokens=invalid),self.assertRaises(ValueError):
                    await client.chat_json(
                        "system","user",max_tokens=invalid
                    )

        request=created[0].posts[0][1]["json"]
        self.assertEqual(request["temperature"],0.35)
        self.assertEqual(request["max_tokens"],800)
        self.assertEqual(len(created[0].posts),1)

    async def test_azure_keeps_output_cap_but_omits_model_field(self):
        created=[]

        def factory(**_kwargs):
            instance=RecordingAsyncClient()
            created.append(instance)
            return instance

        with (
            patch(
                "app.llm.client.LLM_ALLOWED_BASE_URLS",
                ["https://tenant.openai.azure.com"],
            ),
            patch("app.llm.client.httpx.AsyncClient",side_effect=factory),
        ):
            client=LLMClient(
                api_key="azure-secret",
                provider="azure_openai",
                base_url="https://tenant.openai.azure.com",
                model="credit planner",
                api_version="2025-01-01-preview",
            )
            await client.chat_json(
                "system","user",temperature=0.2,max_tokens=600
            )

        url,request=created[0].posts[0]
        self.assertIn("/credit%20planner/chat/completions",url)
        self.assertNotIn("model",request["json"])
        self.assertEqual(request["json"]["temperature"],0.2)
        self.assertEqual(request["json"]["max_tokens"],600)
        self.assertEqual(request["headers"]["api-key"],"azure-secret")

    async def test_failed_request_releases_lease_for_shutdown(self):
        class TimeoutClient(RecordingAsyncClient):
            async def post(self,_url,**_kwargs):
                raise httpx.ReadTimeout("timed out")

        shared=TimeoutClient()
        with patch(
            "app.llm.client.httpx.AsyncClient",return_value=shared
        ):
            with self.assertRaises(LLMTransientError):
                await self.client().chat_json("system","user")
            await asyncio.wait_for(aclose_llm_http_clients(),1)
        self.assertTrue(shared.closed)

    async def test_cancelled_shutdown_still_finishes_client_close(self):
        close_started=asyncio.Event()
        allow_close=asyncio.Event()

        class SlowCloseClient(RecordingAsyncClient):
            async def aclose(self):
                close_started.set()
                await allow_close.wait()
                self.closed=True

        created=[]

        def factory(**_kwargs):
            instance=SlowCloseClient() if not created else RecordingAsyncClient()
            created.append(instance)
            return instance

        with patch("app.llm.client.httpx.AsyncClient",side_effect=factory):
            client=self.client()
            await client.chat_json("system","user")
            closing=asyncio.create_task(aclose_llm_http_clients())
            await asyncio.wait_for(close_started.wait(),1)
            closing.cancel()
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            allow_close.set()
            with self.assertRaises(asyncio.CancelledError):
                await closing
            self.assertTrue(created[0].closed)

            await client.chat_json("system","new pool")
            self.assertEqual(len(created),2)

    async def test_close_without_client_is_idempotent(self):
        await aclose_llm_http_clients()
        await aclose_llm_http_clients()


if __name__=="__main__":
    unittest.main()
