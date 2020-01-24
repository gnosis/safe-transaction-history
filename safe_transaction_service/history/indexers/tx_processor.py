from abc import ABC, abstractmethod
from logging import getLogger
from typing import Union

from django.db import transaction

from hexbytes import HexBytes
from web3 import Web3
from web3._utils.events import EventLogErrorFlags

from gnosis.eth import EthereumClient
from gnosis.eth.constants import NULL_ADDRESS
from gnosis.eth.contracts import get_safe_contract, get_safe_V1_0_0_contract
from gnosis.safe import SafeTx
from gnosis.safe.safe_signature import SafeSignature

from ..models import (InternalTxDecoded, MultisigConfirmation,
                      MultisigTransaction, SafeContract, SafeStatus)

logger = getLogger(__name__)


class TxProcessor(ABC):
    @abstractmethod
    def process_decoded_transaction(self, internal_tx_decoded: InternalTxDecoded) -> bool:
        pass


class SafeTxProcessor(TxProcessor):
    """
    Processor for txs on Safe Contracts v0.0.1 - v1.0.0
    """

    def __init__(self, ethereum_client: EthereumClient):
        self.safe_tx_failure_events = [get_safe_V1_0_0_contract(Web3()).events.ExecutionFailed(),
                                       get_safe_contract(Web3()).events.ExecutionFailure()]
        self.ethereum_client = ethereum_client

    def is_failed(self, tx_hash: Union[str, bytes], safe_tx_hash: Union[str, bytes]) -> bool:
        # TODO Store logs when storing the receipt
        # TODO Move this function to `Safe` in gnosis-py
        safe_tx_hash = HexBytes(safe_tx_hash)
        tx_receipt = self.ethereum_client.get_transaction_receipt(tx_hash)
        for safe_tx_failure_event in self.safe_tx_failure_events:
            for decoded_event in safe_tx_failure_event.processReceipt(tx_receipt, errors=EventLogErrorFlags.Discard):
                if decoded_event['args']['txHash'] == safe_tx_hash:
                    return True
        return False

    @transaction.atomic
    def process_decoded_transaction(self, internal_tx_decoded: InternalTxDecoded) -> bool:
        """
        Decode internal tx and creates needed models
        :param internal_tx_decoded: InternalTxDecoded to process. It will be set as `processed`
        :return: True if tx could be processed, False otherwise
        """
        function_name = internal_tx_decoded.function_name
        arguments = internal_tx_decoded.arguments
        internal_tx = internal_tx_decoded.internal_tx
        contract_address = internal_tx._from
        master_copy = internal_tx.to
        processed_successfully = True
        if function_name == 'setup' and contract_address != NULL_ADDRESS:
            owners = arguments['_owners']
            threshold = arguments['_threshold']
            _, created = SafeContract.objects.get_or_create(address=contract_address,
                                                            defaults={
                                                                'ethereum_tx': internal_tx.ethereum_tx,
                                                                'erc20_block_number': internal_tx.ethereum_tx.block_id,
                                                            })
            if created:
                logger.info('Found new Safe=%s', contract_address)
            SafeStatus.objects.create(internal_tx=internal_tx,
                                      address=contract_address, owners=owners, threshold=threshold,
                                      nonce=0, master_copy=master_copy)
        elif function_name in ('addOwnerWithThreshold', 'removeOwner', 'removeOwnerWithThreshold'):
            safe_status = SafeStatus.objects.last_for_address(contract_address)
            safe_status.threshold = arguments['_threshold']
            owner = arguments['owner']
            try:
                if function_name == 'addOwnerWithThreshold':
                    safe_status.owners.append(owner)
                else:  # removeOwner, removeOwnerWithThreshold
                    safe_status.owners.remove(owner)
            except ValueError:
                logger.error('Error processing trace=%s for contract=%s with tx-hash=%s',
                             internal_tx.trace_address, contract_address,
                             internal_tx.ethereum_tx_id)
            safe_status.store_new(internal_tx)
        elif function_name == 'swapOwner':
            old_owner = arguments['oldOwner']
            new_owner = arguments['newOwner']
            safe_status = SafeStatus.objects.last_for_address(contract_address)
            safe_status.owners.remove(old_owner)
            safe_status.owners.append(new_owner)
            safe_status.store_new(internal_tx)
        elif function_name == 'changeThreshold':
            safe_status = SafeStatus.objects.last_for_address(contract_address)
            safe_status.threshold = arguments['_threshold']
            safe_status.store_new(internal_tx)
        elif function_name == 'changeMasterCopy':
            # TODO Ban address if it doesn't have a valid master copy
            safe_status = SafeStatus.objects.last_for_address(contract_address)
            safe_status.master_copy = arguments['_masterCopy']
            safe_status.store_new(internal_tx)
        elif function_name == 'approveHash':
            multisig_transaction_hash = arguments['hashToApprove']
            ethereum_tx = internal_tx.ethereum_tx
            owner = internal_tx.get_previous_trace()._from
            (multisig_confirmation,
             created) = MultisigConfirmation.objects.get_or_create(multisig_transaction_hash=multisig_transaction_hash,
                                                                   owner=owner,
                                                                   defaults={
                                                                       'ethereum_tx': ethereum_tx,
                                                                   })
            if not created and not multisig_confirmation.ethereum_tx_id:
                multisig_confirmation.ethereum_tx = ethereum_tx
                multisig_confirmation.save()
        elif function_name == 'execTransaction':
            safe_status = SafeStatus.objects.last_for_address(contract_address)
            nonce = safe_status.nonce
            if 'baseGas' in arguments:  # `dataGas` was renamed to `baseGas` in v1.0.0
                base_gas = arguments['baseGas']
                safe_version = '1.0.0'
            else:
                base_gas = arguments['dataGas']
                safe_version = '0.0.1'
            safe_tx = SafeTx(None, contract_address, arguments['to'], arguments['value'], arguments['data'],
                             arguments['operation'], arguments['safeTxGas'], base_gas,
                             arguments['gasPrice'], arguments['gasToken'], arguments['refundReceiver'],
                             HexBytes(arguments['signatures']), safe_nonce=nonce, safe_version=safe_version)
            safe_tx_hash = safe_tx.safe_tx_hash

            ethereum_tx = internal_tx.ethereum_tx

            # Remove existing transaction with same nonce in case of bad indexing (one of the master copies can be
            # outdated and a tx with a wrong nonce could be indexed)
            MultisigTransaction.objects.filter(
                ethereum_tx=ethereum_tx,
                nonce=safe_tx.safe_nonce,
                safe=contract_address
            ).exclude(
                safe_tx_hash=safe_tx_hash
            ).delete()

            # Remove old txs not used
            #MultisigTransaction.objects.filter(
            #    ethereum_tx=None,
            #    nonce__lt=safe_tx.safe_nonce,
            #    safe=contract_address
            #).delete()

            failed = self.is_failed(ethereum_tx.tx_hash, safe_tx_hash)
            multisig_tx, created = MultisigTransaction.objects.get_or_create(
                safe_tx_hash=safe_tx_hash,
                defaults={
                    'safe': contract_address,
                    'ethereum_tx': ethereum_tx,
                    'to': safe_tx.to,
                    'value': safe_tx.value,
                    'data': safe_tx.data if safe_tx.data else None,
                    'operation': safe_tx.operation,
                    'safe_tx_gas': safe_tx.safe_tx_gas,
                    'base_gas': safe_tx.base_gas,
                    'gas_price': safe_tx.gas_price,
                    'gas_token': safe_tx.gas_token,
                    'refund_receiver': safe_tx.refund_receiver,
                    'nonce': safe_tx.safe_nonce,
                    'signatures': safe_tx.signatures,
                    'failed': failed,
                })
            if not created and not multisig_tx.ethereum_tx_id:
                multisig_tx.ethereum_tx = ethereum_tx
                multisig_tx.failed = failed
                multisig_tx.signatures = HexBytes(arguments['signatures'])
                multisig_tx.save(update_fields=['ethereum_tx', 'failed', 'signatures'])

            for safe_signature in SafeSignature.parse_signatures(safe_tx.signatures, safe_tx_hash):
                multisig_confirmation, _ = MultisigConfirmation.objects.get_or_create(
                    multisig_transaction_hash=safe_tx_hash,
                    owner=safe_signature.owner,
                    defaults={
                        'ethereum_tx': None,
                        'multisig_transaction': multisig_tx,
                        'signature': safe_signature.signature,
                    }
                )
                if multisig_confirmation.signature != safe_signature.signature:
                    multisig_confirmation.signature = safe_signature.signature
                    multisig_confirmation.save(update_fields=['signature'])

            safe_status.nonce = nonce + 1
            safe_status.store_new(internal_tx)
        elif function_name == 'execTransactionFromModule':
            # No side effects or nonce increasing, but trace will be set as processed
            pass
        else:
            processed_successfully = False
        internal_tx_decoded.set_processed()
        return processed_successfully
