import unittest
import calc


class TestCalc(unittest.TestCase):
    def test_add_sub(self):
        self.assertEqual(calc.add(2, 3), 5)
        self.assertEqual(calc.subtract(5, 2), 3)

    def test_divide_zero(self):
        with self.assertRaises(ValueError):
            calc.divide(10, 0)

    def test_clamp(self):
        self.assertEqual(calc.clamp(-5, 0, 10), 0)
        self.assertEqual(calc.clamp(25, 0, 10), 10)
        self.assertEqual(calc.clamp(5, 0, 10), 5)

    def test_mean_empty(self):
        self.assertEqual(calc.mean([]), 0.0)
        self.assertAlmostEqual(calc.mean([2, 4]), 3.0)

    def test_median_even(self):
        self.assertAlmostEqual(calc.median([1, 2, 3, 4]), 2.5)
        self.assertAlmostEqual(calc.median([1, 9, 3]), 3.0)

    def test_normalize(self):
        self.assertEqual(calc.normalize([10, 20, 30]), [0.0, 0.5, 1.0])

    def test_parse_csv_ints(self):
        self.assertEqual(calc.parse_csv_ints(" 1, 2 , 3, "), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
