"""Two-step shortcuts: Super + Alt + Space, then N."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shortcuts

SEQ_N = shortcuts.make_combo(["SUPER", "ALT"], "space", "n")
FIRST = shortcuts.make_combo(["SUPER", "ALT"], "space")


def state(*items):
    apps = {}
    for app_id, name, combo in items:
        entry = apps.setdefault(app_id, {"name": name, "launch": [f"/x/{app_id}.desktop"],
                                         "shortcuts": [], "disabled": []})
        entry["shortcuts"].append({**combo, "replaces": "", "action": ""})
    return {"apps": apps}


class TwoStepTests(unittest.TestCase):
    def test_combo_shape_label_and_identity(self):
        self.assertEqual(SEQ_N, {"mods": ["SUPER", "ALT"], "key": "space", "then": "N"})
        self.assertEqual(shortcuts.combo_label(SEQ_N), "Super + Alt + Space, N")
        self.assertNotEqual(shortcuts.combo_id(SEQ_N), shortcuts.combo_id(FIRST))
        self.assertEqual(shortcuts.leader(SEQ_N), FIRST)
        self.assertIsNone(shortcuts.make_combo(["SUPER"], "A", "not a key"))

    def test_collisions(self):
        seq_m = shortcuts.make_combo(["SUPER", "ALT"], "space", "M")
        self.assertTrue(shortcuts.collides(SEQ_N, FIRST))
        self.assertTrue(shortcuts.collides(FIRST, SEQ_N))
        self.assertFalse(shortcuts.collides(SEQ_N, seq_m))  # they share the first step
        self.assertTrue(shortcuts.collides(SEQ_N, dict(SEQ_N)))

    def test_classification(self):
        bindings = shortcuts.Bindings(state(("zen", "Zen", SEQ_N)), live=[], user=([], set()), defaults=[])
        # The same second key belongs to Zen; another one is free and shares the first step.
        self.assertEqual(bindings.classify(SEQ_N, "other"), (shortcuts.LAUNCHER, "Zen"))
        self.assertEqual(bindings.classify(SEQ_N, "zen")[0], shortcuts.SAME)
        self.assertEqual(bindings.classify(shortcuts.make_combo(["SUPER", "ALT"], "space", "M"), "other"),
                         (shortcuts.FREE, ""))
        self.assertEqual(bindings.classify_first_step(FIRST), (shortcuts.FREE, ""))
        # A one-step shortcut on the first step would break the two-step one.
        level, owner = bindings.classify(FIRST, "other")
        self.assertEqual(level, shortcuts.LAUNCHER)
        self.assertIn("Zen", owner)

    def test_first_step_taken_by_omarchy_default(self):
        live = [{"mask": 72, "key": "space", "description": "Apps menu"}]
        bindings = shortcuts.Bindings({"apps": {}}, live=live, user=([], set()), defaults=[])
        self.assertEqual(bindings.classify(SEQ_N, "zen"), (shortcuts.OMARCHY, "Apps menu"))

    def test_render_lua_builds_one_submap_per_first_step(self):
        st = state(("zen", "Zen", SEQ_N), ("aether", "Aether", shortcuts.make_combo(["SUPER", "ALT"], "space", "M")))
        lua = shortcuts.render_lua(st)
        self.assertEqual(lua.count("hl.define_submap(\"simple-launcher-super-alt-space\""), 1)
        self.assertIn(shortcuts.CAPTURE_LUA, lua)
        self.assertIn('hl.unbind("SUPER + ALT + space")', lua)
        self.assertIn('hl.bind("SUPER + ALT + space", hl.dsp.submap("simple-launcher-super-alt-space")', lua)
        self.assertIn('hl.bind("N", function()', lua)
        self.assertIn('hl.exec_cmd("uwsm-app -- /x/zen.desktop")', lua)
        self.assertIn('hl.dispatch(hl.dsp.submap("reset"))', lua)
        self.assertIn('hl.bind("catchall", hl.dsp.submap("reset"))', lua)
        self.assertNotIn("o.bind(", lua)

    def test_render_lua_quotes_second_step_commands(self):
        st = state(("x", 'Evil "name"', SEQ_N))
        st["apps"]["x"]["launch"] = ["/tmp/a b/$(x).desktop"]
        lua = shortcuts.render_lua(st)
        self.assertIn("""hl.exec_cmd("uwsm-app -- '/tmp/a b/$(x).desktop'")""", lua)
        self.assertIn('description = "Evil \\"name\\""', lua)

    def test_state_keeps_second_key(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(shortcuts, "STATE_FILE", Path(tmp) / "s.json"):
            shortcuts.STATE_FILE.write_text(json.dumps(state(("zen", "Zen", SEQ_N))))
            loaded = shortcuts.load_state()
            shortcuts.STATE_FILE.write_text(json.dumps(state(("zen", "Zen", {**SEQ_N, "then": "bad key"}))))
            dropped = shortcuts.load_state()
        self.assertEqual(loaded["apps"]["zen"]["shortcuts"][0]["then"], "N")
        self.assertEqual(dropped["apps"], {})

    def test_owned_ids_include_first_steps(self):
        ids = shortcuts.owned_ids(state(("zen", "Zen", SEQ_N)))
        self.assertIn(shortcuts.combo_id(FIRST), ids)
        self.assertIn(shortcuts.combo_id(SEQ_N), ids)


if __name__ == "__main__":
    unittest.main()
