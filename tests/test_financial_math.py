"""Exact financial arithmetic contract shared by finance/reporting modules."""
import unittest

from ai_earn.financial_math import net_cash_paise


class FinancialMathTests(unittest.TestCase):
    def test_integer_paise_components_produce_exact_net(self):
        self.assertEqual(
            net_cash_paise(
                income_paise=12345,
                refunds_paise=2300,
                fees_paise=125,
                expenses_paise=4000,
            ),
            5920,
        )

    def test_net_can_be_negative_when_outflows_exceed_income(self):
        self.assertEqual(
            net_cash_paise(
                income_paise=0,
                refunds_paise=10,
                fees_paise=5,
                expenses_paise=20,
            ),
            -35,
        )

    def test_large_integer_totals_are_not_float_rounded(self):
        amount = 10**18 + 7
        self.assertEqual(
            net_cash_paise(amount, 0, 0, 1),
            amount - 1,
        )

    def test_invalid_component_types_and_negative_components_are_rejected(self):
        for invalid in (True, 1.5, "100", -1, None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    net_cash_paise(invalid, 0, 0, 0)


if __name__ == "__main__":
    unittest.main()
