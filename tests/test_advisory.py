import json
import unittest

from hermes_evolution.advisory import plan, tool_handler


class AdvisoryTests(unittest.TestCase):
    def test_plan_has_bounded_effort_and_evidence_keys(self):
        quick = plan("research", "quick")
        deep = plan("research", "deep")
        self.assertLess(quick["suggested_budget"]["search_queries"], deep["suggested_budget"]["search_queries"])
        self.assertIn("verification", deep["parallel_roles"][1])
        self.assertIn("primary URL", deep["deliverable"]["findings"])

    def test_delegation_routes_durable_work_to_kanban(self):
        self.assertEqual(plan("delegation")["native_surface"], "delegate_task")
        self.assertEqual(plan("delegation", durable=True)["native_surface"], "kanban")

    def test_tool_has_no_arbitrary_commands_or_profile_inputs(self):
        self.assertIn("error", json.loads(tool_handler({"kind": "operations", "command": "anything"})))
        self.assertIn("error", json.loads(tool_handler({"kind": "delegation", "durable": "false"})))
        self.assertEqual(json.loads(tool_handler({"kind": "operations"}, session_id="not-retained"))["kind"], "operations")
