from django.test import TestCase

from hexbytes import HexBytes

from gnosis.eth.constants import NULL_ADDRESS
from gnosis.eth.contracts import get_safe_V1_3_0_contract
from gnosis.safe.tests.safe_test_case import SafeTestCaseMixin

from ..indexers.safe_events_indexer import SafeEventsIndexer
from ..indexers.tx_processor import SafeTxProcessor
from ..models import InternalTx, InternalTxDecoded, SafeStatus
from .factories import SafeL2MasterCopyFactory


class TestSafeEventsIndexer(SafeTestCaseMixin, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.safe_events_indexer = SafeEventsIndexer(cls.ethereum_client, confirmations=0)
        cls.safe_tx_processor = SafeTxProcessor(cls.ethereum_client)

    def test_safe_events_indexer(self):
        owners = [self.ethereum_test_account.address]
        threshold = 1
        to = NULL_ADDRESS
        data = b''
        fallback_handler = NULL_ADDRESS
        payment_token = NULL_ADDRESS
        payment = 0
        payment_receiver = NULL_ADDRESS
        initializer = HexBytes(
            self.safe_contract_V1_3_0.functions.setup(
                owners, threshold, to, data, fallback_handler, payment_token,
                payment, payment_receiver
            ).buildTransaction({'gas': 1, 'gasPrice': 1})['data']
        )
        block_number = self.ethereum_client.current_block_number
        SafeL2MasterCopyFactory(initial_block_number=block_number, tx_block_number=block_number)
        ethereum_tx_sent = self.proxy_factory.deploy_proxy_contract(
            self.ethereum_test_account, self.safe_contract_V1_3_0.address,
            initializer=initializer
        )
        safe_address = ethereum_tx_sent.contract_address
        safe_contract = get_safe_V1_3_0_contract(self.w3, safe_address)
        self.assertEqual(safe_contract.functions.VERSION().call(), '1.3.0')

        self.assertEqual(InternalTx.objects.count(), 0)
        self.assertEqual(InternalTxDecoded.objects.count(), 0)
        self.assertEqual(self.safe_events_indexer.start(), 1)
        self.assertEqual(InternalTx.objects.count(), 1)
        self.assertEqual(InternalTxDecoded.objects.count(), 1)

        internal_txs_decoded = InternalTxDecoded.objects.pending_for_safes()
        self.assertEqual(SafeStatus.objects.count(), 0)
        self.safe_tx_processor.process_decoded_transactions(internal_txs_decoded)
        safe_status = SafeStatus.objects.get()
        self.assertEqual(safe_status.master_copy, NULL_ADDRESS)
        self.assertEqual(safe_status.owners, owners)
        self.assertEqual(safe_status.threshold, threshold)
