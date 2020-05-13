import logging
from unittest import mock

from django.urls import reverse

from eth_account import Account
from hexbytes import HexBytes
from rest_framework import status
from rest_framework.test import APITestCase
from web3 import Web3

from gnosis.eth.ethereum_client import Erc20Info
from gnosis.safe import Safe
from gnosis.safe.safe_signature import SafeSignature, SafeSignatureType
from gnosis.safe.tests.safe_test_case import SafeTestCaseMixin

from ..helpers import DelegateSignatureHelper
from ..models import (MultisigConfirmation, MultisigTransaction,
                      SafeContractDelegate)
from ..serializers import TransferType
from ..services import BalanceService
from .factories import (EthereumEventFactory, EthereumTxFactory,
                        InternalTxFactory, ModuleTransactionFactory,
                        MultisigConfirmationFactory,
                        MultisigTransactionFactory,
                        SafeContractDelegateFactory, SafeContractFactory,
                        SafeStatusFactory)

logger = logging.getLogger(__name__)


class TestViews(SafeTestCaseMixin, APITestCase):
    def test_all_transactions_view(self):
        safe_address = Account.create().address
        response = self.client.get(reverse('v1:all-transactions', args=(safe_address,)))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)
        self.assertEqual(len(response.data['results']), 0)

        # Factories create the models using current datetime, so as the txs are returned sorted they should be
        # in the reverse order that they were created
        multisig_transaction = MultisigTransactionFactory(safe=safe_address)
        module_transaction = ModuleTransactionFactory(safe=safe_address)
        internal_tx_in = InternalTxFactory(to=safe_address, value=4)
        internal_tx_out = InternalTxFactory(_from=safe_address, value=5)  # Should not appear
        erc20_transfer_in = EthereumEventFactory(to=safe_address)
        erc20_transfer_out = EthereumEventFactory(from_=safe_address)  # Should not appear
        another_multisig_transaction = MultisigTransactionFactory(safe=safe_address)
        another_safe_multisig_transaction = MultisigTransactionFactory()  # Should not appear, it's for another Safe
        response = self.client.get(reverse('v1:all-transactions', args=(safe_address,)))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 5)
        self.assertEqual(len(response.data['results']), 5)
        transfers_not_empty = [False,  # Multisig transaction, no transfer
                               True,  # Erc transfer in
                               True,  # internal tx in
                               False,  # Module transaction
                               False,  # Multisig transaction
                               ]
        for transfer_not_empty, transaction in zip(transfers_not_empty, response.data['results']):
            self.assertEqual(bool(transaction['transfers']), transfer_not_empty)
            self.assertTrue(transaction['tx_type'])

        # Test pagination
        response = self.client.get(reverse('v1:all-transactions', args=(safe_address,)) + '?limit=3')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 5)
        self.assertEqual(len(response.data['results']), 3)

        response = self.client.get(reverse('v1:all-transactions', args=(safe_address,)) + '?limit=3&offset=3')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 5)
        self.assertEqual(len(response.data['results']), 2)

        # Add transfer out for the module transaction and transfer in for the multisig transaction
        erc20_transfer_out = EthereumEventFactory(from_=safe_address,
                                                  ethereum_tx=module_transaction.internal_tx.ethereum_tx)
        internal_tx_in = InternalTxFactory(to=safe_address, value=8,
                                           ethereum_tx=multisig_transaction.ethereum_tx)
        response = self.client.get(reverse('v1:all-transactions', args=(safe_address,)))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 6)  # FIXME
        self.assertEqual(len(response.data['results']), 5)
        transfers_not_empty = [False,  # Multisig transaction, no transfer
                               True,  # Erc transfer in
                               True,  # internal tx in
                               True,  # Module transaction
                               True,  # Multisig transaction
                               ]
        for transfer_not_empty, transaction in zip(transfers_not_empty, response.data['results']):
            self.assertEqual(bool(transaction['transfers']), transfer_not_empty)

    def test_get_module_transactions(self):
        safe_address = Account.create().address
        response = self.client.get(reverse('v1:module-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        module_transaction = ModuleTransactionFactory(safe=safe_address)
        response = self.client.get(reverse('v1:module-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['safe'], module_transaction.safe)
        self.assertEqual(response.data['results'][0]['module'], module_transaction.module)

    def test_get_multisig_transaction(self):
        safe_tx_hash = Web3.keccak(text='gnosis').hex()
        response = self.client.get(reverse('v1:multisig-transaction', args=(safe_tx_hash,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        add_owner_with_threshold_data = HexBytes('0x0d582f130000000000000000000000001b9a0da11a5cace4e7035993cbb2e4'
                                                 'b1b3b164cf000000000000000000000000000000000000000000000000000000'
                                                 '0000000001')
        multisig_tx = MultisigTransactionFactory(data=add_owner_with_threshold_data)
        safe_tx_hash = multisig_tx.safe_tx_hash
        response = self.client.get(reverse('v1:multisig-transaction', args=(safe_tx_hash,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['confirmations']), 0)
        self.assertTrue(Web3.isChecksumAddress(response.data['executor']))
        self.assertEqual(response.data['transaction_hash'], multisig_tx.ethereum_tx.tx_hash)
        self.assertEqual(response.data['origin'], multisig_tx.origin)
        self.assertEqual(response.data['data_decoded'], {'addOwnerWithThreshold': [{'name': 'owner',
                                                                                    'type': 'address',
                                                                                    'value': '0x1b9a0DA11a5caCE4e703599'
                                                                                             '3Cbb2E4B1B3b164Cf'},
                                                                                   {'name': '_threshold',
                                                                                    'type': 'uint256',
                                                                                    'value': 1}]
                                                         })
        # Test camelCase
        self.assertEqual(response.json()['transactionHash'], multisig_tx.ethereum_tx.tx_hash)

    def test_get_multisig_transactions(self):
        safe_address = Account.create().address
        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        multisig_tx = MultisigTransactionFactory(safe=safe_address)
        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['count_unique_nonce'], 1)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(len(response.data['results'][0]['confirmations']), 0)
        self.assertTrue(Web3.isChecksumAddress(response.data['results'][0]['executor']))
        self.assertEqual(response.data['results'][0]['transaction_hash'], multisig_tx.ethereum_tx.tx_hash)
        # Test camelCase
        self.assertEqual(response.json()['results'][0]['transactionHash'], multisig_tx.ethereum_tx.tx_hash)
        # Check Etag header
        self.assertTrue(response['Etag'])

        MultisigConfirmationFactory(multisig_transaction=multisig_tx)
        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(len(response.data['results'][0]['confirmations']), 1)

        MultisigTransactionFactory(safe=safe_address, nonce=multisig_tx.nonce)
        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(response.data['count_unique_nonce'], 1)

    def test_get_multisig_transactions_filters(self):
        safe_address = Account.create().address
        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        multisig_transaction = MultisigTransactionFactory(safe=safe_address, nonce=0, ethereum_tx=None)
        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)) + '?nonce=0',
                                   format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

        response = self.client.get(reverse('v1:multisig-transactions',
                                           args=(safe_address,)) + f'?to=0x2a',
                                   format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['to'][0], 'Enter a valid checksummed Ethereum Address.')

        response = self.client.get(reverse('v1:multisig-transactions',
                                           args=(safe_address,)) + f'?to={multisig_transaction.to}',
                                   format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)) + '?nonce=1',
                                   format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)) + '?executed=true',
                                   format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)) + '?executed=false',
                                   format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

        response = self.client.get(reverse('v1:multisig-transactions',
                                           args=(safe_address,)) + '?has_confirmations=True', format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 0)

        MultisigConfirmationFactory(multisig_transaction=multisig_transaction)
        response = self.client.get(reverse('v1:multisig-transactions',
                                           args=(safe_address,)) + '?has_confirmations=True', format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

    def test_post_multisig_transactions(self):
        safe_owner_1 = Account.create()
        safe_create2_tx = self.deploy_test_safe(owners=[safe_owner_1.address])
        safe_address = safe_create2_tx.safe_address
        safe = Safe(safe_address, self.ethereum_client)

        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        to = Account.create().address
        data = {"to": to,
                "value": 100000000000000000,
                "data": None,
                "operation": 0,
                "nonce": 0,
                "safeTxGas": 0,
                "baseGas": 0,
                "gasPrice": 0,
                "gasToken": "0x0000000000000000000000000000000000000000",
                "refundReceiver": "0x0000000000000000000000000000000000000000",
                # "contractTransactionHash": "0x1c2c77b29086701ccdda7836c399112a9b715c6a153f6c8f75c84da4297f60d3",
                "sender": safe_owner_1.address,
                }
        safe_tx = safe.build_multisig_tx(data['to'], data['value'], data['data'], data['operation'],
                                         data['safeTxGas'], data['baseGas'], data['gasPrice'],
                                         data['gasToken'],
                                         data['refundReceiver'], safe_nonce=data['nonce'])
        data['contractTransactionHash'] = safe_tx.safe_tx_hash.hex()
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)
        self.assertIsNone(response.data['results'][0]['executor'])
        self.assertEqual(len(response.data['results'][0]['confirmations']), 0)

        # Test confirmation with signature
        data['signature'] = safe_owner_1.signHash(safe_tx.safe_tx_hash)['signature'].hex()
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(len(response.data['results'][0]['confirmations']), 1)
        self.assertEqual(response.data['results'][0]['confirmations'][0]['signature'], data['signature'])

        # Sign with a different user that sender
        random_user_account = Account.create()
        data['signature'] = random_user_account.signHash(safe_tx.safe_tx_hash)['signature'].hex()
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertIn(f'Signer={random_user_account.address} is not an owner', response.data['non_field_errors'][0])
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Use random user as sender (not owner)
        del data['signature']
        data['sender'] = random_user_account.address
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertIn(f'Sender={random_user_account.address} is not an owner', response.data['non_field_errors'][0])
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_post_executed_transaction(self):
        safe_owner_1 = Account.create()
        safe_create2_tx = self.deploy_test_safe(owners=[safe_owner_1.address])
        safe_address = safe_create2_tx.safe_address
        safe = Safe(safe_address, self.ethereum_client)

        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        to = Account.create().address
        data = {"to": to,
                "value": 100000000000000000,
                "data": None,
                "operation": 0,
                "nonce": 0,
                "safeTxGas": 0,
                "baseGas": 0,
                "gasPrice": 0,
                "gasToken": "0x0000000000000000000000000000000000000000",
                "refundReceiver": "0x0000000000000000000000000000000000000000",
                # "contractTransactionHash": "0x1c2c77b29086701ccdda7836c399112a9b715c6a153f6c8f75c84da4297f60d3",
                "sender": safe_owner_1.address,
                }
        safe_tx = safe.build_multisig_tx(data['to'], data['value'], data['data'], data['operation'],
                                         data['safeTxGas'], data['baseGas'], data['gasPrice'],
                                         data['gasToken'],
                                         data['refundReceiver'], safe_nonce=data['nonce'])
        data['contractTransactionHash'] = safe_tx.safe_tx_hash.hex()
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        multisig_transaction = MultisigTransaction.objects.first()
        multisig_transaction.ethereum_tx = EthereumTxFactory()
        multisig_transaction.save(update_fields=['ethereum_tx'])
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn(f'Tx with safe-tx-hash={data["contractTransactionHash"]} '
                      f'for safe={safe.address} was already executed in '
                      f'tx-hash={multisig_transaction.ethereum_tx_id}',
                      response.data['non_field_errors'])

        # Check another tx with same nonce
        data['to'] = Account.create().address
        safe_tx = safe.build_multisig_tx(data['to'], data['value'], data['data'], data['operation'],
                                         data['safeTxGas'], data['baseGas'], data['gasPrice'],
                                         data['gasToken'],
                                         data['refundReceiver'], safe_nonce=data['nonce'])
        data['contractTransactionHash'] = safe_tx.safe_tx_hash.hex()
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn(f'Tx with nonce={safe_tx.safe_nonce} for safe={safe.address} '
                      f'already executed in tx-hash={multisig_transaction.ethereum_tx_id}',
                      response.data['non_field_errors'])

        # Successfully insert tx with nonce=1
        data['nonce'] = 1
        safe_tx = safe.build_multisig_tx(data['to'], data['value'], data['data'], data['operation'],
                                         data['safeTxGas'], data['baseGas'], data['gasPrice'],
                                         data['gasToken'],
                                         data['refundReceiver'], safe_nonce=data['nonce'])
        data['contractTransactionHash'] = safe_tx.safe_tx_hash.hex()
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_post_multisig_transactions_with_origin(self):
        safe_owner_1 = Account.create()
        safe_create2_tx = self.deploy_test_safe(owners=[safe_owner_1.address])
        safe_address = safe_create2_tx.safe_address
        safe = Safe(safe_address, self.ethereum_client)

        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        to = Account.create().address
        data = {"to": to,
                "value": 100000000000000000,
                "data": None,
                "operation": 0,
                "nonce": 0,
                "safeTxGas": 0,
                "baseGas": 0,
                "gasPrice": 0,
                "gasToken": "0x0000000000000000000000000000000000000000",
                "refundReceiver": "0x0000000000000000000000000000000000000000",
                # "contractTransactionHash": "0x1c2c77b29086701ccdda7836c399112a9b715c6a153f6c8f75c84da4297f60d3",
                "sender": safe_owner_1.address,
                "origin": 'Testing origin field',
                }

        safe_tx = safe.build_multisig_tx(data['to'], data['value'], data['data'], data['operation'],
                                         data['safeTxGas'], data['baseGas'], data['gasPrice'],
                                         data['gasToken'],
                                         data['refundReceiver'], safe_nonce=data['nonce'])
        data['contractTransactionHash'] = safe_tx.safe_tx_hash.hex()
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        multisig_tx_db = MultisigTransaction.objects.get(safe_tx_hash=safe_tx.safe_tx_hash)
        self.assertEqual(multisig_tx_db.origin, data['origin'])

    def test_post_mulisig_transactions_with_multiple_signatures(self):
        safe_owners = [Account.create() for _ in range(4)]
        safe_owner_addresses = [s.address for s in safe_owners]
        safe_create2_tx = self.deploy_test_safe(owners=safe_owner_addresses, threshold=3)
        safe_address = safe_create2_tx.safe_address
        safe = Safe(safe_address, self.ethereum_client)

        response = self.client.get(reverse('v1:multisig-transactions', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        to = Account.create().address
        data = {"to": to,
                "value": 100000000000000000,
                "data": None,
                "operation": 0,
                "nonce": 0,
                "safeTxGas": 0,
                "baseGas": 0,
                "gasPrice": 0,
                "gasToken": "0x0000000000000000000000000000000000000000",
                "refundReceiver": "0x0000000000000000000000000000000000000000",
                # "contractTransactionHash": "0x1c2c77b29086701ccdda7836c399112a9b715c6a153f6c8f75c84da4297f60d3",
                "sender": safe_owners[0].address,
                "origin": 'Testing origin field',
                }

        safe_tx = safe.build_multisig_tx(data['to'], data['value'], data['data'], data['operation'],
                                         data['safeTxGas'], data['baseGas'], data['gasPrice'],
                                         data['gasToken'],
                                         data['refundReceiver'], safe_nonce=data['nonce'])
        safe_tx_hash = safe_tx.safe_tx_hash
        data['contractTransactionHash'] = safe_tx_hash.hex()
        data['signature'] = b''.join([safe_owner.signHash(safe_tx_hash)['signature']
                                      for safe_owner in safe_owners]).hex()
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        multisig_tx_db = MultisigTransaction.objects.get(safe_tx_hash=safe_tx.safe_tx_hash)
        self.assertEqual(multisig_tx_db.origin, data['origin'])

        multisig_confirmations = MultisigConfirmation.objects.filter(multisig_transaction_hash=safe_tx_hash)
        self.assertEqual(len(multisig_confirmations), len(safe_owners))
        for multisig_confirmation in multisig_confirmations:
            safe_signatures = SafeSignature.parse_signature(multisig_confirmation.signature, safe_tx_hash)
            self.assertEqual(len(safe_signatures), 1)
            safe_signature = safe_signatures[0]
            self.assertEqual(safe_signature.signature_type, SafeSignatureType.EOA)
            self.assertIn(safe_signature.owner, safe_owner_addresses)
            safe_owner_addresses.remove(safe_signature.owner)

    def test_post_mulisig_transactions_with_delegate(self):
        safe_owners = [Account.create() for _ in range(4)]
        safe_owner_addresses = [s.address for s in safe_owners]
        safe_delegate = Account.create()
        safe_create2_tx = self.deploy_test_safe(owners=safe_owner_addresses, threshold=3)
        safe_address = safe_create2_tx.safe_address
        safe = Safe(safe_address, self.ethereum_client)

        self.assertEqual(MultisigTransaction.objects.count(), 0)

        to = Account.create().address
        data = {"to": to,
                "value": 100000000000000000,
                "data": None,
                "operation": 0,
                "nonce": 0,
                "safeTxGas": 0,
                "baseGas": 0,
                "gasPrice": 0,
                "gasToken": "0x0000000000000000000000000000000000000000",
                "refundReceiver": "0x0000000000000000000000000000000000000000",
                # "contractTransactionHash": "0x1c2c77b29086701ccdda7836c399112a9b715c6a153f6c8f75c84da4297f60d3",
                "sender": safe_owners[0].address,
                "origin": 'Testing origin field',
                }

        safe_tx = safe.build_multisig_tx(data['to'], data['value'], data['data'], data['operation'],
                                         data['safeTxGas'], data['baseGas'], data['gasPrice'],
                                         data['gasToken'],
                                         data['refundReceiver'], safe_nonce=data['nonce'])
        safe_tx_hash = safe_tx.safe_tx_hash
        data['contractTransactionHash'] = safe_tx_hash.hex()
        data['signature'] = safe_delegate.signHash(safe_tx_hash)['signature'].hex()

        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn(f'Signer={safe_delegate.address} is not an owner or delegate',
                      response.data['non_field_errors'][0])

        data['sender'] = safe_delegate.address
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn(f'Sender={safe_delegate.address} is not an owner or delegate',
                      response.data['non_field_errors'][0])

        # Add delegate
        SafeContractDelegateFactory(safe_contract__address=safe_address, delegate=safe_delegate.address)
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(MultisigTransaction.objects.count(), 1)
        self.assertEqual(MultisigConfirmation.objects.count(), 0)

        data['signature'] = data['signature'] + data['signature'][2:]
        response = self.client.post(reverse('v1:multisig-transactions', args=(safe_address,)), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn('Just one signature is expected if using delegates', response.data['non_field_errors'][0])

    def test_safe_balances_view(self):
        safe_address = Account.create().address
        response = self.client.get(reverse('v1:safe-balances', args=(safe_address, )), format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        SafeContractFactory(address=safe_address)
        value = 7
        self.send_ether(safe_address, 7)
        response = self.client.get(reverse('v1:safe-balances', args=(safe_address, )), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertIsNone(response.data[0]['token_address'])
        self.assertEqual(response.data[0]['balance'], str(value))

        tokens_value = 12
        erc20 = self.deploy_example_erc20(tokens_value, safe_address)
        response = self.client.get(reverse('v1:safe-balances', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        EthereumEventFactory(address=erc20.address, to=safe_address)
        response = self.client.get(reverse('v1:safe-balances', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(response.json(), [{'tokenAddress': None, 'balance': str(value), 'token': None},
                                                {'tokenAddress': erc20.address, 'balance': str(tokens_value),
                                                 'token': {'name': erc20.functions.name().call(),
                                                           'symbol': erc20.functions.symbol().call(),
                                                           'decimals': erc20.functions.decimals().call()}}])

    @mock.patch.object(BalanceService, 'get_token_info',  autospec=True)
    @mock.patch.object(BalanceService, 'get_token_eth_value', return_value=0.4, autospec=True)
    @mock.patch.object(BalanceService, 'get_eth_usd_price', return_value=123.4, autospec=True)
    def test_safe_balances_usd_view(self, get_eth_usd_price_mock, get_token_eth_value_mock, get_token_info_mock):
        erc20_info = Erc20Info('UXIO', 'UXI', 18)
        get_token_info_mock.return_value = erc20_info

        safe_address = Account.create().address
        response = self.client.get(reverse('v1:safe-balances-usd', args=(safe_address, )), format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        SafeContractFactory(address=safe_address)
        value = 7
        self.send_ether(safe_address, 7)
        response = self.client.get(reverse('v1:safe-balances-usd', args=(safe_address, )), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertIsNone(response.data[0]['token_address'])
        self.assertEqual(response.data[0]['balance'], str(value))

        tokens_value = int(12 * 1e18)
        erc20 = self.deploy_example_erc20(tokens_value, safe_address)
        response = self.client.get(reverse('v1:safe-balances-usd', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        EthereumEventFactory(address=erc20.address, to=safe_address)
        response = self.client.get(reverse('v1:safe-balances-usd', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(response.json(), [{'tokenAddress': None, 'token': None, 'balance': str(value),
                                                 'balanceUsd': "0.0"},  # 7 wei is rounded to 0.0
                                                {'tokenAddress': erc20.address,
                                                 'token': erc20_info._asdict(),
                                                 'balance': str(tokens_value),
                                                 'balanceUsd': str(round(123.4 * 0.4 * (tokens_value / 1e18), 4))}])

    def test_get_safe_delegate_list(self):
        safe_address = Account.create().address
        response = self.client.get(reverse('v1:safe-delegates', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        safe_contract_delegate = SafeContractDelegateFactory()
        safe_address = safe_contract_delegate.safe_contract_id
        response = self.client.get(reverse('v1:safe-delegates', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(response.data['count'], 1)
        result = response.data['results'][0]
        self.assertEqual(result['delegate'], safe_contract_delegate.delegate)
        self.assertEqual(result['delegator'], safe_contract_delegate.delegator)
        self.assertEqual(result['label'], safe_contract_delegate.label)

        safe_contract_delegate = SafeContractDelegateFactory(safe_contract=safe_contract_delegate.safe_contract)
        response = self.client.get(reverse('v1:safe-delegates', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)

        # A different non related Safe should not increase the number
        SafeContractDelegateFactory()
        response = self.client.get(reverse('v1:safe-delegates', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)

    def test_post_safe_delegate(self):
        safe_address = Account.create().address
        delegate_address = Account.create().address
        label = 'Saul Goodman'
        response = self.client.post(reverse('v1:safe-delegates', args=(safe_address, )), format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)  # Data is missing

        data = {
            'delegate': delegate_address,
            'label': label,
            'signature': '0x' + '1' * 130,
        }

        owner_account = Account.create()
        safe_address = self.deploy_test_safe(owners=[owner_account.address]).safe_address
        response = self.client.post(reverse('v1:safe-delegates', args=(safe_address, )), format='json', data=data)
        self.assertIn(f'Safe={safe_address} does not exist', response.data['non_field_errors'][0])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        safe_contract = SafeContractFactory(address=safe_address)
        response = self.client.post(reverse('v1:safe-delegates', args=(safe_address, )), format='json', data=data)
        self.assertIn('Signing owner is not an owner of the Safe', response.data['non_field_errors'][0])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertEqual(SafeContractDelegate.objects.count(), 0)
        hash_to_sign = DelegateSignatureHelper.calculate_hash(delegate_address)
        data['signature'] = owner_account.signHash(hash_to_sign)['signature'].hex()
        response = self.client.post(reverse('v1:safe-delegates', args=(safe_address, )), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(SafeContractDelegate.objects.count(), 1)
        safe_contract_delegate = SafeContractDelegate.objects.first()
        self.assertEqual(safe_contract_delegate.delegate, delegate_address)
        self.assertEqual(safe_contract_delegate.delegator, owner_account.address)
        self.assertEqual(safe_contract_delegate.label, label)

        label = 'Jimmy McGill'
        data['label'] = label
        response = self.client.post(reverse('v1:safe-delegates', args=(safe_address, )), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(SafeContractDelegate.objects.count(), 1)
        safe_contract_delegate.refresh_from_db()
        self.assertEqual(safe_contract_delegate.label, label)

        another_label = 'Kim Wexler'
        another_delegate_address = Account.create().address
        data = {
            'delegate': another_delegate_address,
            'label': another_label,
            'signature': owner_account.signHash(DelegateSignatureHelper.calculate_hash(another_delegate_address,
                                                                                         eth_sign=True)
                                                )['signature'].hex(),
        }
        response = self.client.post(reverse('v1:safe-delegates', args=(safe_address, )), format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.get(reverse('v1:safe-delegates', args=(safe_address,)), format='json')
        self.assertCountEqual(response.data['results'],
                              [
                                  {
                                      'delegate': delegate_address,
                                      'delegator': owner_account.address,
                                      'label': label,
                                  },
                                  {
                                      'delegate': another_delegate_address,
                                      'delegator': owner_account.address,
                                      'label': another_label,
                                  },
                              ])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(SafeContractDelegate.objects.count(), 2)
        self.assertCountEqual(SafeContractDelegate.objects.get_delegates_for_safe(safe_address),
                              [delegate_address, another_delegate_address])

    def test_delete_safe_delegate(self):
        safe_address = Account.create().address
        delegate_address = Account.create().address
        response = self.client.delete(reverse('v1:safe-delegate', args=(safe_address, delegate_address)), format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)  # Data is missing

        data = {
            'delegate': delegate_address,
            'signature': '0x' + '1' * 130,
        }
        response = self.client.delete(reverse('v1:safe-delegate', args=(safe_address, delegate_address)),
                                      format='json', data=data)
        self.assertIn(f'Safe={safe_address} does not exist', response.data['non_field_errors'][0])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        owner_account = Account.create()
        safe_address = self.deploy_test_safe(owners=[owner_account.address]).safe_address
        response = self.client.delete(reverse('v1:safe-delegate', args=(safe_address, delegate_address)),
                                      format='json', data=data)
        self.assertIn(f'Safe={safe_address} does not exist', response.data['non_field_errors'][0])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        safe_contract = SafeContractFactory(address=safe_address)
        response = self.client.delete(reverse('v1:safe-delegate', args=(safe_address, delegate_address)),
                                      format='json', data=data)
        self.assertIn('Signing owner is not an owner of the Safe', response.data['non_field_errors'][0])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        hash_to_sign = DelegateSignatureHelper.calculate_hash(delegate_address)
        data['signature'] = owner_account.signHash(hash_to_sign)['signature'].hex()
        response = self.client.delete(reverse('v1:safe-delegate', args=(safe_address, delegate_address)),
                                      format='json', data=data)
        self.assertIn('Not found', response.data['detail'])
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        SafeContractDelegateFactory(safe_contract=safe_contract, delegate=delegate_address)
        SafeContractDelegateFactory(safe_contract=safe_contract, delegate=Account.create().address)
        self.assertEqual(SafeContractDelegate.objects.count(), 2)
        response = self.client.delete(reverse('v1:safe-delegate', args=(safe_address, delegate_address)),
                                      format='json', data=data)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(SafeContractDelegate.objects.count(), 1)

    def test_incoming_transfers_view(self):
        safe_address = Account.create().address
        response = self.client.get(reverse('v1:incoming-transfers', args=(safe_address, )))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)
        self.assertEqual(len(response.data['results']), 0)

        value = 2
        InternalTxFactory(to=safe_address, value=0)
        internal_tx = InternalTxFactory(to=safe_address, value=value)
        InternalTxFactory(to=Account.create().address, value=value)
        response = self.client.get(reverse('v1:incoming-transfers', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['value'], str(value))
        # Check Etag header
        self.assertTrue(response['Etag'])

        # Test filters
        block_number = internal_tx.ethereum_tx.block_id
        url = reverse('v1:incoming-transfers', args=(safe_address,)) + f'?block_number__gt={block_number}'
        response = self.client.get(url, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        # Add from tx. Result should be the same
        InternalTxFactory(_from=safe_address, value=value)
        response = self.client.get(reverse('v1:incoming-transfers', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['value'], str(value))

        url = reverse('v1:incoming-transfers', args=(safe_address,)) + f'?block_number__gt={block_number - 1}'
        response = self.client.get(url, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        token_value = 6
        ethereum_erc_20_event = EthereumEventFactory(to=safe_address, value=token_value)
        response = self.client.get(reverse('v1:incoming-transfers', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(response.json()['results'], [
            {'type': TransferType.ERC20_TRANSFER.name,
             'executionDate': ethereum_erc_20_event.ethereum_tx.block.timestamp.isoformat().replace('+00:00', 'Z'),
             'transactionHash': ethereum_erc_20_event.ethereum_tx_id,
             'blockNumber': ethereum_erc_20_event.ethereum_tx.block_id,
             'to': safe_address,
             'value': str(token_value),
             'tokenId': None,
             'tokenAddress': ethereum_erc_20_event.address,
             'from': ethereum_erc_20_event.arguments['from']
             },
            {'type': TransferType.ETHER_TRANSFER.name,
             'executionDate': internal_tx.ethereum_tx.block.timestamp.isoformat().replace('+00:00', 'Z'),
             'transactionHash': internal_tx.ethereum_tx_id,
             'blockNumber': internal_tx.ethereum_tx.block_id,
             'to': safe_address,
             'value': str(value),
             'tokenId': None,
             'tokenAddress': None,
             'from': internal_tx._from,
             },
        ])

        token_id = 17
        ethereum_erc_721_event = EthereumEventFactory(to=safe_address, value=token_id, erc721=True)
        response = self.client.get(reverse('v1:incoming-transfers', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 3)
        self.assertEqual(response.json()['results'], [
            {'type': TransferType.ERC721_TRANSFER.name,
             'executionDate': ethereum_erc_721_event.ethereum_tx.block.timestamp.isoformat().replace('+00:00', 'Z'),
             'transactionHash': ethereum_erc_721_event.ethereum_tx_id,
             'blockNumber': ethereum_erc_721_event.ethereum_tx.block_id,
             'to': safe_address,
             'value': None,
             'tokenId': str(token_id),
             'tokenAddress': ethereum_erc_721_event.address,
             'from': ethereum_erc_721_event.arguments['from']
             },
            {'type': TransferType.ERC20_TRANSFER.name,
             'executionDate': ethereum_erc_20_event.ethereum_tx.block.timestamp.isoformat().replace('+00:00', 'Z'),
             'transactionHash': ethereum_erc_20_event.ethereum_tx_id,
             'blockNumber': ethereum_erc_20_event.ethereum_tx.block_id,
             'to': safe_address,
             'value': str(token_value),
             'tokenId': None,
             'tokenAddress': ethereum_erc_20_event.address,
             'from': ethereum_erc_20_event.arguments['from']
             },
            {'type': TransferType.ETHER_TRANSFER.name,
             'executionDate': internal_tx.ethereum_tx.block.timestamp.isoformat().replace('+00:00', 'Z'),
             'transactionHash': internal_tx.ethereum_tx_id,
             'blockNumber': internal_tx.ethereum_tx.block_id,
             'to': safe_address,
             'value': str(value),
             'tokenId': None,
             'tokenAddress': None,
             'from': internal_tx._from,
             },
        ])

    def test_transfers_view(self):
        safe_address = Account.create().address
        response = self.client.get(reverse('v1:transfers', args=(safe_address, )))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)
        self.assertEqual(len(response.data['results']), 0)

        value = 2
        InternalTxFactory(to=safe_address, value=0)
        internal_tx = InternalTxFactory(to=safe_address, value=value)
        InternalTxFactory(to=Account.create().address, value=value)
        response = self.client.get(reverse('v1:incoming-transfers', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['value'], str(value))
        # Check Etag header
        self.assertTrue(response['Etag'])

        # Test filters
        block_number = internal_tx.ethereum_tx.block_id
        url = reverse('v1:transfers', args=(safe_address,)) + f'?block_number__gt={block_number}'
        response = self.client.get(url, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

        url = reverse('v1:transfers', args=(safe_address,)) + f'?block_number__gt={block_number - 1}'
        response = self.client.get(url, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Add from tx
        internal_tx_2 = InternalTxFactory(_from=safe_address, value=value)
        response = self.client.get(reverse('v1:transfers', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(response.data['results'][0]['value'], str(value))
        self.assertEqual(response.data['results'][1]['value'], str(value))

        token_value = 6
        ethereum_erc_20_event = EthereumEventFactory(to=safe_address, value=token_value)
        ethereum_erc_20_event_2 = EthereumEventFactory(from_=safe_address, value=token_value)
        response = self.client.get(reverse('v1:transfers', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 4)
        expected_results = [
            {'type': TransferType.ERC20_TRANSFER.name,
             'executionDate': ethereum_erc_20_event_2.ethereum_tx.block.timestamp.isoformat().replace('+00:00', 'Z'),
             'blockNumber': ethereum_erc_20_event_2.ethereum_tx.block_id,
             'transactionHash': ethereum_erc_20_event_2.ethereum_tx_id,
             'to': ethereum_erc_20_event_2.arguments['to'],
             'value': str(token_value),
             'tokenId': None,
             'tokenAddress': ethereum_erc_20_event_2.address,
             'from': safe_address,
             },
            {'type': TransferType.ERC20_TRANSFER.name,
             'executionDate': ethereum_erc_20_event.ethereum_tx.block.timestamp.isoformat().replace('+00:00', 'Z'),
             'blockNumber': ethereum_erc_20_event.ethereum_tx.block_id,
             'transactionHash': ethereum_erc_20_event.ethereum_tx_id,
             'to': safe_address,
             'value': str(token_value),
             'tokenId': None,
             'tokenAddress': ethereum_erc_20_event.address,
             'from': ethereum_erc_20_event.arguments['from']
             },
            {'type': TransferType.ETHER_TRANSFER.name,
             'executionDate': internal_tx_2.ethereum_tx.block.timestamp.isoformat().replace('+00:00', 'Z'),
             'blockNumber': internal_tx_2.ethereum_tx.block_id,
             'transactionHash': internal_tx_2.ethereum_tx_id,
             'to': internal_tx_2.to,
             'value': str(value),
             'tokenId': None,
             'tokenAddress': None,
             'from': safe_address,
             },
            {'type': TransferType.ETHER_TRANSFER.name,
             'executionDate': internal_tx.ethereum_tx.block.timestamp.isoformat().replace('+00:00', 'Z'),
             'blockNumber': internal_tx.ethereum_tx.block_id,
             'transactionHash': internal_tx.ethereum_tx_id,
             'to': safe_address,
             'value': str(value),
             'tokenId': None,
             'tokenAddress': None,
             'from': internal_tx._from,
             },
        ]
        self.assertEqual(response.json()['results'], expected_results)

        token_id = 17
        ethereum_erc_721_event = EthereumEventFactory(to=safe_address, value=token_id, erc721=True)
        ethereum_erc_721_event_2 = EthereumEventFactory(from_=safe_address, value=token_id, erc721=True)
        response = self.client.get(reverse('v1:transfers', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 6)
        expected_results = [
           {'type': TransferType.ERC721_TRANSFER.name,
            'executionDate': ethereum_erc_721_event_2.ethereum_tx.block.timestamp.isoformat().replace(
                '+00:00', 'Z'),
            'transactionHash': ethereum_erc_721_event_2.ethereum_tx_id,
            'blockNumber': ethereum_erc_721_event_2.ethereum_tx.block_id,
            'to': ethereum_erc_721_event_2.arguments['to'],
            'value': None,
            'tokenId': str(token_id),
            'tokenAddress': ethereum_erc_721_event_2.address,
            'from': safe_address,
            },
            {'type': TransferType.ERC721_TRANSFER.name,
             'executionDate': ethereum_erc_721_event.ethereum_tx.block.timestamp.isoformat().replace('+00:00', 'Z'),
             'transactionHash': ethereum_erc_721_event.ethereum_tx_id,
             'blockNumber': ethereum_erc_721_event.ethereum_tx.block_id,
             'to': safe_address,
             'value': None,
             'tokenId': str(token_id),
             'tokenAddress': ethereum_erc_721_event.address,
             'from': ethereum_erc_721_event.arguments['from']
             },
        ] + expected_results
        self.assertEqual(response.json()['results'], expected_results)

    def test_safe_creation_view(self):
        invalid_address = '0x2A'
        response = self.client.get(reverse('v1:safe-creation', args=(invalid_address,)))
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

        owner_address = Account.create().address
        response = self.client.get(reverse('v1:safe-creation', args=(owner_address,)))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        internal_tx = InternalTxFactory(contract_address=owner_address, trace_address='0,0')
        response = self.client.get(reverse('v1:safe-creation', args=(owner_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        created_iso = internal_tx.ethereum_tx.block.timestamp.isoformat().replace('+00:00', 'Z')
        expected = {'created': created_iso,
                    'creator': internal_tx._from,
                    'factory_address': internal_tx._from,
                    'master_copy': None,
                    'setup_data': None,
                    'transaction_hash': internal_tx.ethereum_tx_id}
        self.assertEqual(response.data, expected)

        # Next internal_tx should not alter the result
        next_internal_tx = InternalTxFactory(trace_address='0,0,0', ethereum_tx=internal_tx.ethereum_tx)
        response = self.client.get(reverse('v1:safe-creation', args=(owner_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, expected)

        # Previous internal_tx should change the `creator` and `master_copy` and `setup_data` should appear
        # Taken from rinkeby
        create_test_data = {
            'master_copy': '0xb6029EA3B2c51D09a50B53CA8012FeEB05bDa35A',
            'setup_data': '0xa97ab18a00000000000000000000000000000000000000000000000000000000000000e000000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000016000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000030000000000000000000000006e45d69a383ceca3d54688e833bd0e1388747e6b00000000000000000000000061a0c717d18232711bc788f19c9cd56a43cc88720000000000000000000000007724b234c9099c205f03b458944942bceba134080000000000000000000000000000000000000000000000000000000000000000',
            'data': '0x61b69abd000000000000000000000000b6029ea3b2c51d09a50b53ca8012feeb05bda35a00000000000000000000000000000000000000000000000000000000000000400000000000000000000000000000000000000000000000000000000000000184a97ab18a00000000000000000000000000000000000000000000000000000000000000e000000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000016000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000030000000000000000000000006e45d69a383ceca3d54688e833bd0e1388747e6b00000000000000000000000061a0c717d18232711bc788f19c9cd56a43cc88720000000000000000000000007724b234c9099c205f03b458944942bceba13408000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000',
        }
        create_test_data_2 = {
            'master_copy': '0x34CfAC646f301356fAa8B21e94227e3583Fe3F5F',
            'setup_data': '0xb63e800d0000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000180000000000000000000000000d5d82b6addc9027b22dca772aa68d5d74cdbdf440000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000ac9b6dd409ff10000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000300000000000000000000000085c26101f353f38e45c72d414b44972831f07be3000000000000000000000000235518798770d7336c5c4908dd1019457fea43a10000000000000000000000007f63c25665ea7e85500eaeb806e552e651b07b9d00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000',
            'data': '0x1688f0b900000000000000000000000034cfac646f301356faa8b21e94227e3583fe3f5f0000000000000000000000000000000000000000000000000000000000000060000000000000000000000000000000000000000000000000000002cecc9e861200000000000000000000000000000000000000000000000000000000000001c4b63e800d0000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000180000000000000000000000000d5d82b6addc9027b22dca772aa68d5d74cdbdf440000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000ac9b6dd409ff10000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000300000000000000000000000085c26101f353f38e45c72d414b44972831f07be3000000000000000000000000235518798770d7336c5c4908dd1019457fea43a10000000000000000000000007f63c25665ea7e85500eaeb806e552e651b07b9d0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
        }

        create_cpk_test_data = {
            'master_copy': '0x34CfAC646f301356fAa8B21e94227e3583Fe3F5F',
            'setup_data': '0x5714713d000000000000000000000000ff54516a7bc1c1ea952a688e72d5b93a80620074',
            'data': '0x460868ca00000000000000000000000034cfac646f301356faa8b21e94227e3583fe3f5fcfe33a586323e7325be6aa6ecd8b4600d232a9037e83c8ece69413b777dabe6500000000000000000000000040a930851bd2e590bd5a5c981b436de25742e9800000000000000000000000005ef44de4b98f2bce0e29c344e7b2fb8f0282a0cf000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000e0000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000245714713d000000000000000000000000ff54516a7bc1c1ea952a688e72d5b93a8062007400000000000000000000000000000000000000000000000000000000',
        }

        previous_internal_tx = InternalTxFactory(trace_address='0', ethereum_tx=internal_tx.ethereum_tx)
        for test_data in [create_test_data, create_test_data_2, create_cpk_test_data]:
            previous_internal_tx.data = HexBytes(test_data['data'])
            previous_internal_tx.save(update_fields=['data'])
            response = self.client.get(reverse('v1:safe-creation', args=(owner_address,)), format='json')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            created_iso = internal_tx.ethereum_tx.block.timestamp.isoformat().replace('+00:00', 'Z')
            self.assertEqual(response.data, {'created': created_iso,
                                             'creator': previous_internal_tx._from,
                                             'factory_address': internal_tx._from,
                                             'master_copy': test_data['master_copy'],
                                             'setup_data': test_data['setup_data'],
                                             'transaction_hash': internal_tx.ethereum_tx_id})

    def test_safe_info_view(self):
        invalid_address = '0x2A'
        response = self.client.get(reverse('v1:safe-info', args=(invalid_address,)))
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

        safe_create_tx = self.deploy_test_safe()
        safe_address = safe_create_tx.safe_address
        response = self.client.get(reverse('v1:safe-info', args=(safe_address,)))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        SafeContractFactory(address=safe_address)
        response = self.client.get(reverse('v1:safe-info', args=(safe_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {
            'address': safe_address,
            'nonce': 0,
            'threshold': safe_create_tx.threshold,
            'owners': safe_create_tx.owners,
            'master_copy': safe_create_tx.master_copy_address,
            'modules': [],
            'fallback_handler': safe_create_tx.fallback_handler,
            'version': '1.1.1'})

    def test_owners_view(self):
        invalid_address = '0x2A'
        response = self.client.get(reverse('v1:owners', args=(invalid_address,)))
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

        owner_address = Account.create().address
        response = self.client.get(reverse('v1:owners', args=(owner_address,)))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        safe_status = SafeStatusFactory(owners=[owner_address])
        response = self.client.get(reverse('v1:owners', args=(owner_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(response.data['safes'], [safe_status.address])

        safe_status_2 = SafeStatusFactory(owners=[owner_address])
        SafeStatusFactory()  # Test that other SafeStatus don't appear
        response = self.client.get(reverse('v1:owners', args=(owner_address,)), format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(response.data['safes'], [safe_status.address, safe_status_2.address])
