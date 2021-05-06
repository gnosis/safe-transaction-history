from django.test import TestCase

from eth_account import Account
from hexbytes import HexBytes

from gnosis.eth.constants import NULL_ADDRESS, SENTINEL_ADDRESS
from gnosis.eth.contracts import get_safe_V1_3_0_contract
from gnosis.safe import Safe
from gnosis.safe.tests.safe_test_case import SafeTestCaseMixin

from ..indexers.safe_events_indexer import SafeEventsIndexer
from ..indexers.tx_processor import SafeTxProcessor
from ..models import (EthereumTxCallType, InternalTx, InternalTxDecoded,
                      InternalTxType, SafeStatus)
from .factories import SafeL2MasterCopyFactory


class TestSafeEventsIndexer(SafeTestCaseMixin, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.safe_events_indexer = SafeEventsIndexer(cls.ethereum_client, confirmations=0)
        cls.safe_tx_processor = SafeTxProcessor(cls.ethereum_client)

    def test_safe_events_indexer(self):
        owner_account_1 = self.ethereum_test_account
        owners = [owner_account_1.address]
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
        safe = Safe(safe_address, self.ethereum_client)
        safe_contract = get_safe_V1_3_0_contract(self.w3, safe_address)
        self.assertEqual(safe_contract.functions.VERSION().call(), '1.3.0')

        self.assertEqual(InternalTx.objects.count(), 0)
        self.assertEqual(InternalTxDecoded.objects.count(), 0)
        self.assertEqual(self.safe_events_indexer.start(), 1)
        self.assertEqual(InternalTx.objects.count(), 1)
        self.assertEqual(InternalTxDecoded.objects.count(), 1)

        txs_decoded_queryset = InternalTxDecoded.objects.pending_for_safes()
        self.assertEqual(SafeStatus.objects.count(), 0)
        self.safe_tx_processor.process_decoded_transactions(txs_decoded_queryset.all())
        self.assertEqual(SafeStatus.objects.count(), 1)
        safe_status = SafeStatus.objects.get()
        self.assertEqual(safe_status.master_copy, NULL_ADDRESS)
        self.assertEqual(safe_status.owners, owners)
        self.assertEqual(safe_status.threshold, threshold)
        self.assertEqual(safe_status.nonce, 0)
        self.assertEqual(safe_status.enabled_modules, [])
        self.assertIsNone(safe_status.guard)

        # Add an owner but don't update the threshold (nonce: 0)
        owner_account_2 = Account.create()
        data = HexBytes(
            self.safe_contract_V1_3_0.functions.addOwnerWithThreshold(
                owner_account_2.address,
                1
            ).buildTransaction({'gas': 1, 'gasPrice': 1})['data']
        )

        multisig_tx = safe.build_multisig_tx(safe_address, 0, data)
        multisig_tx.sign(owner_account_1.key)
        multisig_tx.execute(self.ethereum_test_account.key)
        # Process events: SafeMultiSigTransaction, AddedOwner, ExecutionSuccess
        self.assertEqual(self.safe_events_indexer.start(), 3)
        self.safe_tx_processor.process_decoded_transactions(txs_decoded_queryset.all())
        # Add one SafeStatus increasing the nonce and another one adding the owner
        self.assertEqual(SafeStatus.objects.count(), 3)
        safe_status = SafeStatus.objects.last_for_address(safe_address)  # Processed execTransaction and addOwner
        self.assertCountEqual(safe_status.owners, [owner_account_1.address, owner_account_2.address])
        self.assertEqual(safe_status.nonce, 1)

        safe_status = SafeStatus.objects.sorted_by_internal_tx()[1]  # Just processed execTransaction
        self.assertCountEqual(safe_status.owners, [owner_account_1.address])
        self.assertEqual(safe_status.nonce, 1)

        # Remove an owner (nonce: 1)
        data = HexBytes(
            self.safe_contract_V1_3_0.functions.removeOwner(
                SENTINEL_ADDRESS,
                owner_account_2.address,
                1
            ).buildTransaction({'gas': 1, 'gasPrice': 1})['data']
        )

        multisig_tx = safe.build_multisig_tx(safe_address, 0, data)
        multisig_tx.sign(owner_account_1.key)
        multisig_tx.execute(self.ethereum_test_account.key)
        # Process events: SafeMultiSigTransaction, RemovedOwner, ExecutionSuccess
        self.assertEqual(self.safe_events_indexer.start(), 3)
        self.safe_tx_processor.process_decoded_transactions(txs_decoded_queryset.all())
        # Add one SafeStatus increasing the nonce and another one removing the owner
        self.assertEqual(SafeStatus.objects.count(), 5)
        safe_status = SafeStatus.objects.last_for_address(safe_address)  # Processed execTransaction and addOwner
        self.assertCountEqual(safe_status.owners, [owner_account_1.address])
        self.assertEqual(safe_status.nonce, 2)

        safe_status = SafeStatus.objects.sorted_by_internal_tx()[1]  # Just processed execTransaction
        self.assertCountEqual(safe_status.owners, [owner_account_1.address, owner_account_2.address])
        self.assertEqual(safe_status.nonce, 2)

        # Enable module (nonce: 2)
        module_address = Account.create().address
        data = HexBytes(
            self.safe_contract_V1_3_0.functions.enableModule(
                module_address
            ).buildTransaction({'gas': 1, 'gasPrice': 1})['data']
        )

        multisig_tx = safe.build_multisig_tx(safe_address, 0, data)
        multisig_tx.sign(owner_account_1.key)
        multisig_tx.execute(self.ethereum_test_account.key)
        # Process events: SafeMultiSigTransaction, EnabledModule, ExecutionSuccess
        self.assertEqual(self.safe_events_indexer.start(), 3)
        self.safe_tx_processor.process_decoded_transactions(txs_decoded_queryset.all())
        # Add one SafeStatus increasing the nonce and another one enabling the module
        self.assertEqual(SafeStatus.objects.count(), 7)
        safe_status = SafeStatus.objects.last_for_address(safe_address)  # Processed execTransaction and enableModule
        self.assertEqual(safe_status.enabled_modules, [module_address])
        self.assertEqual(safe_status.nonce, 3)

        safe_status = SafeStatus.objects.sorted_by_internal_tx()[1]  # Just processed execTransaction
        self.assertEqual(safe_status.enabled_modules, [])
        self.assertEqual(safe_status.nonce, 3)

        # Check SafeReceived (ether received) on Safe
        value = 1256
        self.ethereum_client.get_transaction_receipt(self.send_ether(safe_address, value))
        # Process events: SafeReceived
        self.assertEqual(self.safe_events_indexer.start(), 1)
        self.safe_tx_processor.process_decoded_transactions(txs_decoded_queryset.all())
        # Add one SafeStatus increasing the nonce and another one changing the guard
        self.assertTrue(
            InternalTx.objects.filter(
                value=value,
                tx_type=InternalTxType.CALL.value,
                call_type=EthereumTxCallType.CALL.value
            ).exists()
        )

        # Set guard (nonce: 3) INVALIDATES SAFE, as no more transactions can be done
        guard_address = Account.create().address
        data = HexBytes(
            self.safe_contract_V1_3_0.functions.setGuard(
                guard_address
            ).buildTransaction({'gas': 1, 'gasPrice': 1})['data']
        )

        multisig_tx = safe.build_multisig_tx(safe_address, 0, data)
        multisig_tx.sign(owner_account_1.key)
        multisig_tx.execute(self.ethereum_test_account.key)
        # Process events: SafeMultiSigTransaction, ChangedGuard, ExecutionSuccess
        self.assertEqual(self.safe_events_indexer.start(), 3)
        self.safe_tx_processor.process_decoded_transactions(txs_decoded_queryset.all())
        # Add one SafeStatus increasing the nonce and another one changing the guard
        self.assertEqual(SafeStatus.objects.count(), 9)
        safe_status = SafeStatus.objects.last_for_address(safe_address)  # Processed execTransaction and setGuard
        self.assertEqual(safe_status.guard, guard_address)
        self.assertEqual(safe_status.nonce, 4)

        safe_status = SafeStatus.objects.sorted_by_internal_tx()[1]  # Just processed execTransaction
        self.assertEqual(safe_status.nonce, 4)
        self.assertIsNone(safe_status.guard)
