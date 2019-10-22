from abc import ABC, abstractmethod
from logging import getLogger

from django.db import transaction

from hexbytes import HexBytes

from gnosis.eth.constants import NULL_ADDRESS
from gnosis.safe import SafeTx

from ..models import (InternalTxDecoded, MultisigConfirmation,
                      MultisigTransaction, SafeStatus)

logger = getLogger(__name__)


class TxProcessor(ABC):
    @abstractmethod
    def process_decoded_transaction(self, internal_tx_decoded: InternalTxDecoded) -> bool:
        pass


class SafeTxProcessor(TxProcessor):
    """
    Processor for txs on Safe Contracts v0.0.1 - v1.0.0
    """
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
        processed = True
        if function_name == 'setup':
            # We need to get the master_copy from the next trace `DELEGATE_CALL`
            #next_trace = internal_tx.get_next_trace()
            #if next_trace:
            #    master_copy = next_trace.to
            #else:
            #    master_copy = NULL_ADDRESS
            owners = arguments['_owners']
            threshold = arguments['_threshold']
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
            multisig_tx, created = MultisigTransaction.objects.get_or_create(
                safe_tx_hash=safe_tx_hash,
                defaults={
                    'safe': contract_address,
                    'ethereum_tx': ethereum_tx,
                    'to': safe_tx.to,
                    'value': safe_tx.value,
                    'data': safe_tx.data,
                    'operation': safe_tx.operation,
                    'safe_tx_gas': safe_tx.safe_tx_gas,
                    'base_gas': safe_tx.base_gas,
                    'gas_price': safe_tx.gas_price,
                    'gas_token': safe_tx.gas_token,
                    'refund_receiver': safe_tx.refund_receiver,
                    'nonce': safe_tx.safe_nonce,
                    'signatures': HexBytes(arguments['signatures']),
                })
            if not created and not multisig_tx.ethereum_tx:
                multisig_tx.ethereum_tx = ethereum_tx
                multisig_tx.signatures = arguments['signatures']
                multisig_tx.save(update_fields=['ethereum_tx', 'signatures'])

            safe_status.nonce = nonce + 1
            safe_status.store_new(internal_tx)
        elif function_name == 'approveHash':
            multisig_transaction_hash = arguments['hashToApprove']
            ethereum_tx = internal_tx.ethereum_tx
            owner = internal_tx._from
            try:
                multisig_transaction = MultisigTransaction.objects.get(
                    safe_tx_hash=multisig_transaction_hash
                )
            except MultisigTransaction.DoesNotExist:
                multisig_transaction = None

            MultisigConfirmation.objects.get_or_create(multisig_transaction_hash=multisig_transaction_hash,
                                                       owner=owner,
                                                       defaults={
                                                           'multisig_transaction': multisig_transaction,
                                                           'ethereum_tx': ethereum_tx,
                                                       })
        elif function_name == 'execTransactionFromModule':
            # No side effects or nonce increasing, but trace will be set as processed
            pass
        else:
            processed = False
        if processed:
            internal_tx_decoded.set_processed()
        return processed
