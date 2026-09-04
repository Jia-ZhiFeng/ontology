import unittest

from app.agent.conversation import (
    AgentConversationBusyError,
    AgentConversationError,
    AgentConversationStore,
    sanitize_conversation_text,
)


class AgentConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.store = AgentConversationStore(
            max_conversations=2,
            max_turns=2,
            ttl_seconds=30,
            processing_timeout_seconds=10,
            clock=lambda: self.now[0],
            scope_secret=b"test-secret" * 4,
        )

    def begin(self, conversation_id=None, application_id="APP-001"):
        return self.store.begin(
            conversation_id,
            application_id=application_id,
            ontology_version="1.1.0",
        )

    def commit(self, lease, user="查风险", assistant="已生成风险预览"):
        return self.store.commit(
            lease,
            user=user,
            assistant=assistant,
            response_type="ACTION_RUN",
            tool_names=["preview_risk_assessment"],
        )

    def test_single_inflight_turn_prevents_duplicate_index_and_abort_releases_it(self):
        first = self.begin()
        with self.assertRaises(AgentConversationBusyError):
            self.begin(first.conversation_id)

        self.store.abort(first)
        second = self.begin(first.conversation_id)
        self.assertEqual(first.turn_index, 1)
        self.assertEqual(second.turn_index, 2)
        self.commit(second)

    def test_processing_lease_expires_but_stale_lease_cannot_commit(self):
        first = self.begin()
        self.now[0] += 11
        second = self.begin(first.conversation_id)

        with self.assertRaisesRegex(AgentConversationError, "租约"):
            self.commit(first)
        context = self.commit(second)
        self.assertEqual(context[0]["turn_index"], 2)

    def test_ttl_expiry_rejects_resume(self):
        first = self.begin()
        self.commit(first)
        self.now[0] += 31

        with self.assertRaisesRegex(AgentConversationError, "不存在或已过期"):
            self.begin(first.conversation_id)

    def test_context_is_bounded_and_contains_only_safe_fields(self):
        first = self.begin()
        self.commit(
            first,
            user="application_id=APP-001 手机 13800138000 api_key=secret-value",
            assistant="第一轮",
        )
        second = self.begin(first.conversation_id)
        self.commit(second, user="第二轮", assistant="第二轮")
        third = self.begin(first.conversation_id)
        context = self.commit(third, user="第三轮", assistant="第三轮")

        self.assertEqual([turn["turn_index"] for turn in context], [2, 3])
        self.assertEqual(
            set(context[0]),
            {"turn_index", "user", "assistant", "response_type", "tool_names"},
        )
        stored = self.store.inspect(first.conversation_id)
        self.assertNotIn("application_id", stored)
        self.assertNotIn("scope_digest", stored)

    def test_sanitizer_masks_credentials_and_common_identifiers(self):
        safe = sanitize_conversation_text(
            "api_key=sk-live-secret APP-AGENT-001 手机13800138000 "
            "身份证 110101199001011234 姓名：张三 邮箱 user@example.com"
        )

        self.assertNotIn("sk-live-secret", safe)
        self.assertNotIn("13800138000", safe)
        self.assertNotIn("APP-AGENT-001", safe)
        self.assertNotIn("110101199001011234", safe)
        self.assertNotIn("张三", safe)
        self.assertNotIn("user@example.com", safe)
        self.assertIn("[已脱敏]", safe)


if __name__ == "__main__":
    unittest.main()
