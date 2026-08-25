"""
Documentation is shared between Swagger and MCP.

`base/apidocs.py` exists so a developer reading `/api/docs/` and a model reading
the MCP tool list are told the same things about quota and error
codes. Two copies would drift, and the model's copy is the one nobody
proofreads. These tests fail if the two surfaces stop drawing from it.
"""

from django.test import SimpleTestCase, TestCase

from base import apidocs
from mcp_server import protocol
from mcp_server.tools import TOOLS, tool_descriptors


class SharedSourceTests(SimpleTestCase):
    def test_every_error_code_the_api_raises_is_documented(self):
        """A code with no entry here reaches clients with no explanation."""
        from base import exceptions

        raised = {
            value
            for name, value in vars(exceptions).items()
            if name.isupper() and isinstance(value, str) and name == value
        }
        undocumented = raised - set(apidocs.ERROR_CODES)
        self.assertEqual(
            undocumented,
            set(),
            f"error codes missing from base/apidocs.py: {sorted(undocumented)}",
        )

    def test_error_table_renders_requested_codes(self):
        table = apidocs.error_table("SLOT_TAKEN", "RULE_BLOCKED")
        self.assertIn("`SLOT_TAKEN`", table)
        self.assertIn("`RULE_BLOCKED`", table)
        self.assertNotIn("`SUSPENDED`", table)

    def test_error_table_ignores_unknown_codes(self):
        self.assertNotIn("NONSENSE", apidocs.error_table("NONSENSE", "SLOT_TAKEN"))

    def test_fairness_rules_name_the_live_rules(self):
        for rule in ("Quota", "Advance window", "Cancellation cutoff"):
            self.assertIn(rule, apidocs.FAIRNESS_RULES)
        self.assertNotIn("Cooldown", apidocs.FAIRNESS_RULES)

    def test_slot_states_document_every_state(self):
        from laundry.services import slots

        for state in (
            slots.SLOT_FREE,
            slots.SLOT_TAKEN,
            slots.SLOT_MINE,
            slots.SLOT_BLOCKED,
            slots.SLOT_OFFLINE,
            slots.SLOT_RUNNING,
            slots.SLOT_PAST,
        ):
            with self.subTest(state=state):
                self.assertIn(f"`{state}`", apidocs.SLOT_STATES)


class McpUsesSharedDocsTests(SimpleTestCase):
    def test_server_instructions_come_from_apidocs(self):
        self.assertIs(protocol.SERVER_INSTRUCTIONS, apidocs.MCP_SERVER_INSTRUCTIONS)

    def test_instructions_explain_the_fairness_rules(self):
        """
        The model has to know quota exists, or it retries a refused
        booking instead of telling the student why it failed.
        """
        self.assertIn(apidocs.FAIRNESS_RULES.strip(), protocol.SERVER_INSTRUCTIONS)
        # Collapse the hard wrapping before matching prose.
        flowed = " ".join(protocol.SERVER_INSTRUCTIONS.split())
        self.assertIn("Tell the student that reason rather than retrying", flowed)

    def test_every_tool_has_a_shared_note(self):
        self.assertEqual(set(TOOLS), set(apidocs.MCP_TOOL_NOTES))

    def test_tool_descriptors_use_the_shared_notes(self):
        for descriptor in tool_descriptors():
            with self.subTest(tool=descriptor["name"]):
                self.assertEqual(
                    descriptor["description"],
                    apidocs.MCP_TOOL_NOTES[descriptor["name"]],
                )

    def test_tool_descriptions_are_substantial(self):
        """A one-line description is not enough for a model to choose well."""
        for name, note in apidocs.MCP_TOOL_NOTES.items():
            with self.subTest(tool=name):
                self.assertGreater(len(note), 80)

    def test_every_tool_declares_read_write_annotations(self):
        """Claude's directory (and ChatGPT) use these to confirm write actions."""
        readonly = {"find_available_slots", "list_my_bookings"}
        destructive = {"cancel_booking"}
        for descriptor in tool_descriptors():
            with self.subTest(tool=descriptor["name"]):
                annotations = descriptor.get("annotations") or {}
                self.assertIn("readOnlyHint", annotations)
                self.assertIn("destructiveHint", annotations)
                if descriptor["name"] in readonly:
                    self.assertTrue(annotations["readOnlyHint"])
                    self.assertFalse(annotations["destructiveHint"])
                else:
                    self.assertFalse(annotations["readOnlyHint"])
                if descriptor["name"] in destructive:
                    self.assertTrue(annotations["destructiveHint"])


class SwaggerUsesSharedDocsTests(TestCase):
    @staticmethod
    def _schema():
        from drf_spectacular.drainage import GENERATOR_STATS
        from drf_spectacular.generators import SchemaGenerator

        GENERATOR_STATS.reset()
        schema = SchemaGenerator().get_schema(request=None, public=True)
        problems = sorted(GENERATOR_STATS._warn_cache) + sorted(
            GENERATOR_STATS._error_cache
        )
        return schema, problems

    def test_api_description_carries_the_shared_prose(self):
        schema, problems = self._schema()
        self.assertEqual(problems, [])
        description = schema["info"]["description"]
        self.assertIn(apidocs.FAIRNESS_RULES.strip(), description)
        self.assertIn(apidocs.SLOT_STATES.strip(), description)
        self.assertIn(apidocs.BOOKING_FLOW.strip(), description)

    def test_description_points_developers_at_the_mcp_server(self):
        description = self._schema()[0]["info"]["description"]
        self.assertIn("/mcp/", description)

    def test_booking_endpoint_documents_partial_success(self):
        operation = self._schema()[0]["paths"]["/api/v1/bookings"]["post"]
        self.assertIn("independent", operation["description"])
        self.assertIn("SLOT_TAKEN", operation["description"])
        self.assertTrue(operation["summary"])

    def test_booking_endpoint_ships_request_and_response_examples(self):
        operation = self._schema()[0]["paths"]["/api/v1/bookings"]["post"]
        request_examples = operation["requestBody"]["content"][
            "application/json"
        ]["examples"]
        response_examples = operation["responses"]["200"]["content"][
            "application/json"
        ]["examples"]
        self.assertTrue(request_examples)
        self.assertTrue(response_examples)
        # The partial-success example is the one that actually teaches the shape.
        partial = [
            ex
            for ex in response_examples.values()
            if "results" in str(ex.get("value", ""))
        ]
        self.assertTrue(partial, "no per-item results example on POST /bookings")

    def test_slots_endpoint_documents_the_states(self):
        operation = self._schema()[0]["paths"]["/api/v1/machines/{machine_id}/slots"][
            "get"
        ]
        self.assertIn("blocked", operation["description"])
        self.assertTrue(
            operation["responses"]["200"]["content"]["application/json"]["examples"]
        )

    def test_institute_rules_endpoint_explains_the_rules(self):
        operation = self._schema()[0]["paths"]["/api/v1/me/institute"]["get"]
        self.assertIn("Quota", operation["description"])
        self.assertNotIn("Cooldown", operation["description"])
