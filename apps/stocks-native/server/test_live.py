import unittest

from live import parse_minute_payload, parse_quote_payload


class LiveParserTests(unittest.TestCase):
    def test_quote_parser_maps_live_book_and_scales_amounts(self):
        fields = [""] * 50
        fields[1:9] = ["平安银行", "000001", "11.70", "11.61", "11.59", "853038", "452206", "400831"]
        fields[9:29] = ["11.70", "7029", "11.69", "2100", "11.68", "2873", "11.67", "1086", "11.66", "1423",
                           "11.71", "1849", "11.72", "1006", "11.73", "1250", "11.74", "4120", "11.75", "4537"]
        fields[31:50] = ["0.09", "0.78", "11.82", "11.56", "", "", "99992", "1.24", "6.5", "", "", "", "2.24", "100", "200", "0.8", "12.77", "10.45", "1.06"]
        payload = ('v_sz000001="51~' + "~".join(fields[1:]) + '";').encode("gbk")
        result = parse_quote_payload(payload)["000001"]
        self.assertEqual(result["price"], 11.7)
        self.assertEqual(result["turnover"], 999_920_000)
        self.assertEqual(result["bids"][0]["price"], 11.7)
        self.assertEqual(result["asks"][4]["volume"], 4537)

    def test_minute_parser_calculates_incremental_volume_and_average(self):
        payload = {"data": {"sz000001": {"qt": {"sz000001": ["", "", "", "", "11.61"]},
                    "data": {"date": "20260918", "data": [
                        "0930 11.59 100 115900.00", "0931 11.60 160 185500.00"]}}}}
        result = parse_minute_payload(payload, "000001")
        self.assertEqual(result["previousClose"], 11.61)
        self.assertEqual([row["volume"] for row in result["rows"]], [100, 60])
        self.assertAlmostEqual(result["rows"][1]["averagePrice"], 11.594, places=3)


if __name__ == "__main__":
    unittest.main()
