import unittest
from unittest.mock import AsyncMock, Mock
from app_core.channels_batch_submit import submit_once
from app_core.video_batch_contract import VideoBatchError


class SubmitTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_never_reclicks(self):
        click = AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(VideoBatchError) as error:
            await submit_once(persist_intent=Mock(), click=click, readback=AsyncMock(return_value={}))
        self.assertEqual(error.exception.error_code, 'channels_publish_outcome_unknown')
        click.assert_awaited_once()

    async def test_durable_intent_failure_prevents_click(self):
        click = AsyncMock()
        with self.assertRaises(OSError):
            await submit_once(persist_intent=Mock(side_effect=OSError()), click=click, readback=AsyncMock())
        click.assert_not_awaited()

    async def test_late_receipt_after_timeout_is_accepted(self):
        receipt = {'status': 'success', 'platformPostId': 'test-id'}
        result = await submit_once(persist_intent=Mock(), click=AsyncMock(side_effect=TimeoutError()), readback=AsyncMock(return_value=receipt))
        self.assertEqual(result, receipt)

    async def test_navigation_alone_is_not_receipt(self):
        with self.assertRaises(VideoBatchError):
            await submit_once(persist_intent=Mock(), click=AsyncMock(), readback=AsyncMock(return_value={'status':'success'}))
