import tempfile
import unittest

from voice import VoiceSession


class CapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.directory = tempfile.TemporaryDirectory()

        async def emit_json(event):
            self.events.append(event)
            if event.get('type') == 'capability.action':
                self.session.capability_result(event['actionId'], True, 'App 已应用')

        async def emit_audio(_):
            return None

        self.session = VoiceSession('test-device', self.directory.name, None, emit_json, emit_audio)
        self.session.stock_code = '000001'

    async def asyncTearDown(self):
        await self.session.pc.close()
        self.directory.cleanup()

    async def test_annotation_waits_for_client_receipt(self):
        result = await self.session.request_capability({
            'operation': 'add_arrow', 'color': 'red',
            'x': 0.2, 'y': 0.7, 'x2': 0.8, 'y2': 0.3,
        })
        self.assertTrue(result['success'])
        self.assertEqual(result['stockCode'], '000001')
        event = self.events[0]
        self.assertEqual(event['capability'], 'chart.annotation')
        self.assertEqual(event['operation'], 'add_arrow')
        self.assertEqual(event['color'], 'red')

    async def test_annotation_rejects_out_of_range_coordinate(self):
        with self.assertRaisesRegex(ValueError, '0 到 1'):
            await self.session.request_capability({
                'operation': 'add_line', 'x': -0.1, 'y': 0.1, 'x2': 0.8, 'y2': 0.8,
            })


if __name__ == '__main__':
    unittest.main()
