"""Exercise the actual ATH check expression, including its half-tenth boundary."""

import ast
from pathlib import Path
import unittest


class DisplayedATHTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "scripts" / "check_data.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        checks = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "check"
            and len(node.args) == 2
            and "playbook.ath=" in ast.unparse(node.args[1])
        ]
        if len(checks) != 1:
            raise AssertionError("Expected exactly one production ATH consistency check")
        cls.expression = compile(ast.Expression(checks[0].args[0]), str(path), "eval")

    def accepted(self, displayed, prices):
        return eval(
            self.expression,
            {"__builtins__": {}, "abs": abs, "max": max, "round": round},
            {"ath": displayed, "d": {"price": list(enumerate(prices))}},
        )

    def test_actual_failure_half_tenth(self):
        self.assertTrue(self.accepted(round(7773.95, 1), [7722.72, 7773.95]))

    def test_other_rounding_boundaries(self):
        for price in [7773.05, 7773.15, 7773.25, 7773.75, 7773.85, 7741.36]:
            with self.subTest(price=price):
                self.assertTrue(self.accepted(round(price, 1), [7000.0, price]))

    def test_uses_historical_max_not_latest(self):
        self.assertTrue(self.accepted(round(7773.95, 1), [7773.95, 7722.72]))
        self.assertFalse(self.accepted(round(7722.72, 1), [7773.95, 7722.72]))

    def test_wrong_rounded_value_still_rejected(self):
        for displayed in [7773.8, 7774.0, 7000.0, 7773.94, 7773.95]:
            with self.subTest(displayed=displayed):
                self.assertFalse(self.accepted(displayed, [7773.95]))


if __name__ == "__main__":
    unittest.main()
