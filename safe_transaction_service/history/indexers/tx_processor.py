from abc import ABC, abstractmethod
from logging import getLogger
from typing import Dict, List, Optional, Sequence, Union

from django.db import transaction

from eth_utils import event_abi_to_log_topic
from hexbytes import HexBytes
from web3 import Web3

from gnosis.eth import EthereumClient
from gnosis.eth.constants import NULL_ADDRESS
from gnosis.eth.contracts import get_safe_contract, get_safe_V1_0_0_contract
from gnosis.safe import SafeTx
from gnosis.safe.safe_signature import SafeSignature, SafeSignatureApprovedHash

from ..models import (EthereumTx, InternalTx, InternalTxDecoded,
                      ModuleTransaction, MultisigConfirmation,
                      MultisigTransaction, SafeContract, SafeStatus)

logger = getLogger(__name__)


class TxProcessorException(Exception):
    pass


class OwnerCannotBeRemoved(TxProcessorException):
    pass


class SafeTxProcessorProvider:
    def __new__(cls):
        if not hasattr(cls, 'instance'):
            from django.conf import settings
            cls.instance = SafeTxProcessor(EthereumClient(settings.ETHEREUM_TRACING_NODE_URL))
        return cls.instance

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, 'instance'):
            del cls.instance


class TxProcessor(ABC):
    @abstractmethod
    def process_decoded_transaction(self, internal_tx_decoded: InternalTxDecoded) -> bool:
        pass

    def process_decoded_transactions(self, internal_txs_decoded: Sequence[InternalTxDecoded]) -> List[bool]:
        return [self.process_decoded_transaction(decoded_transaction) for decoded_transaction in internal_txs_decoded]


class SafeTxProcessor(TxProcessor):
    """
    Processor for txs on Safe Contracts v0.0.1 - v1.0.0
    """

    def __init__(self, ethereum_client: EthereumClient):
        # This safe_tx_failure events allow us to detect a failed safe transaction
        self.ethereum_client = ethereum_client
        dummy_w3 = Web3()
        self.safe_tx_failure_events = [
            get_safe_V1_0_0_contract(dummy_w3).events.ExecutionFailed(),
            get_safe_contract(dummy_w3).events.ExecutionFailure()
        ]
        self.safe_tx_module_failure_events = [
            get_safe_contract(dummy_w3).events.ExecutionFromModuleFailure()
        ]

        self.safe_tx_failure_events_topics = {
            event_abi_to_log_topic(event.abi) for event in self.safe_tx_failure_events
        }
        self.safe_tx_module_failure_topics = {
            event_abi_to_log_topic(event.abi) for event in self.safe_tx_module_failure_events
        }
        self.safe_status_cache: Dict[str, SafeStatus] = {}

    def clear_cache(self, safe_address: Optional[str] = None):
        if safe_address:
            if safe_address in self.safe_status_cache:
                del self.safe_status_cache[safe_address]
        else:
            self.safe_status_cache.clear()

    def is_failed(self, ethereum_tx: EthereumTx, safe_tx_hash: Union[str, bytes]) -> bool:
        """
        Detects failure events on a Safe Multisig Tx
        :param ethereum_tx:
        :param safe_tx_hash:
        :return: True if a Multisig Transaction is failed, False otherwise
        """
        # TODO Refactor this function to `Safe` in gnosis-py, it doesn't belong here
        safe_tx_hash = HexBytes(safe_tx_hash).hex()
        for log in ethereum_tx.logs:
            if (log['topics'] and log['data']
                    and HexBytes(log['topics'][0]) in self.safe_tx_failure_events_topics
                    and log['data'][:66] == safe_tx_hash):  # 66 is the beginning of the event data, the rest is payment
                return True
        return False

    def is_module_failed(self, ethereum_tx: EthereumTx, module_address: str, safe_address: str) -> bool:
        """
        Detects module failure events on a Safe Module Tx
        :param ethereum_tx:
        :param module_address:
        :param safe_address:
        :return: True if a Module Transaction is failed, False otherwise
        """
        # TODO Refactor this function to `Safe` in gnosis-py, it doesn't belong here
        for log in ethereum_tx.logs:
            if (
                    len(log['topics']) == 2
                    and (log['address'] == safe_address if 'address' in log else True)
                    and HexBytes(log['topics'][0]) in self.safe_tx_module_failure_topics
                    and HexBytes(log['topics'][1])[-20:] == HexBytes(module_address)  # 20 address size in bytes
            ):
                return True
        return False

    def get_last_safe_status_for_address(self, address: str) -> SafeStatus:
        safe_status = self.safe_status_cache.get(address) or SafeStatus.objects.last_for_address(address)
        if not safe_status:
            logger.error('SafeStatus not found for address=%s', address)
        return safe_status

    def remove_owner(self, internal_tx: InternalTx, safe_status: SafeStatus, owner: str):
        """
        :param internal_tx:
        :param safe_status:
        :param owner:
        :return:
        """
        contract_address = internal_tx._from
        try:
            safe_status.owners.remove(owner)
            MultisigConfirmation.objects.remove_unused_confirmations(contract_address, safe_status.nonce, owner)
        except ValueError as e:
            logger.error('Error processing trace=%s for contract=%s with tx-hash=%s. Cannot remove owner=%s',
                         internal_tx.trace_address, contract_address, internal_tx.ethereum_tx_id, owner)
            raise OwnerCannotBeRemoved() from e

    def store_new_safe_status(self, safe_status: SafeStatus, internal_tx: InternalTx) -> SafeStatus:
        safe_status.store_new(internal_tx)
        self.safe_status_cache[safe_status.address] = safe_status
        return self.safe_status_cache[safe_status.address]

    @transaction.atomic
    def process_decoded_transaction(self, internal_tx_decoded: InternalTxDecoded) -> bool:
        processed_successfully = self.__process_decoded_transaction(internal_tx_decoded)
        internal_tx_decoded.set_processed()
        return processed_successfully

    @transaction.atomic
    def process_decoded_transactions(self, internal_txs_decoded: Sequence[InternalTxDecoded]) -> List[bool]:
        """
        Optimize to process multiple transactions in a batch
        :param internal_txs_decoded:
        :return:
        """
        results = [self.__process_decoded_transaction(internal_tx_decoded)
                   for internal_tx_decoded in internal_txs_decoded]

        # Set all as decoded in the same batch
        internal_tx_ids = [internal_tx_decoded.internal_tx_id
                           for internal_tx_decoded in internal_txs_decoded]
        InternalTxDecoded.objects.filter(internal_tx__in=internal_tx_ids).update(processed=True)
        return results

    def __process_decoded_transaction(self, internal_tx_decoded: InternalTxDecoded) -> bool:
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
        if internal_tx.gas_used < 1000:
            # When calling a non existing function, fallback of the proxy does not return any error but we can detect
            # this kind of functions due to little gas used. Some of this transactions get decoded as they were
            # valid in old versions of the proxies, like changes to `setup`
            return False

        processed_successfully = True
        logger.debug('Start processing InternalTxDecoded in tx-hash=%s',
                     HexBytes(internal_tx_decoded.internal_tx.ethereum_tx_id).hex())
        if function_name == 'setup' and contract_address != NULL_ADDRESS:
            logger.debug('Processing Safe setup')
            owners = arguments['_owners']
            threshold = arguments['_threshold']
            fallback_handler = arguments.get('fallbackHandler', NULL_ADDRESS)
            nonce = 0
            try:
                safe_contract: SafeContract = SafeContract.objects.get(address=contract_address)
                if not safe_contract.ethereum_tx_id or not safe_contract.erc20_block_number:
                    safe_contract.ethereum_tx = internal_tx.ethereum_tx
                    safe_contract.erc20_block_number = internal_tx.ethereum_tx.block_id
                    safe_contract.save(update_fields=['ethereum_tx', 'erc20_block_number'])
            except SafeContract.DoesNotExist:
                blocks_one_day = int(24 * 60 * 60 / 15)  # 15 seconds block
                SafeContract.objects.create(address=contract_address,
                                            ethereum_tx=internal_tx.ethereum_tx,
                                            erc20_block_number=max(internal_tx.ethereum_tx.block_id - blocks_one_day,
                                                                   0))
                logger.info('Found new Safe=%s', contract_address)

            SafeStatus.objects.create(internal_tx=internal_tx,
                                      address=contract_address, owners=owners, threshold=threshold,
                                      nonce=nonce, master_copy=master_copy, fallback_handler=fallback_handler)
            self.clear_cache(contract_address)
        elif function_name in ('addOwnerWithThreshold', 'removeOwner', 'removeOwnerWithThreshold'):
            logger.debug('Processing owner/threshold modification')
            safe_status = self.get_last_safe_status_for_address(contract_address)
            safe_status.threshold = arguments['_threshold'] or safe_status.threshold  # Event does not have threshold
            owner = arguments['owner']
            if function_name == 'addOwnerWithThreshold':
                safe_status.owners.append(owner)
            else:  # removeOwner, removeOwnerWithThreshold
                self.remove_owner(internal_tx, safe_status, owner)
            self.store_new_safe_status(safe_status, internal_tx)
        elif function_name == 'swapOwner':
            logger.debug('Processing owner swap')
            old_owner = arguments['oldOwner']
            new_owner = arguments['newOwner']
            safe_status = self.get_last_safe_status_for_address(contract_address)
            self.remove_owner(internal_tx, safe_status, old_owner)
            safe_status.owners.append(new_owner)
            self.store_new_safe_status(safe_status, internal_tx)
        elif function_name == 'changeThreshold':
            logger.debug('Processing threshold change')
            safe_status = self.get_last_safe_status_for_address(contract_address)
            safe_status.threshold = arguments['_threshold']
            self.store_new_safe_status(safe_status, internal_tx)
        elif function_name == 'changeMasterCopy':
            logger.debug('Processing master copy change')
            # TODO Ban address if it doesn't have a valid master copy
            safe_status = self.get_last_safe_status_for_address(contract_address)
            safe_status.master_copy = arguments['_masterCopy']
            self.store_new_safe_status(safe_status, internal_tx)
        elif function_name == 'setFallbackHandler':
            logger.debug('Setting FallbackHandler')
            safe_status = self.get_last_safe_status_for_address(contract_address)
            safe_status.fallback_handler = arguments['handler']
            self.store_new_safe_status(safe_status, internal_tx)
        elif function_name == 'setGuard':
            safe_status = self.get_last_safe_status_for_address(contract_address)
            safe_status.guard = arguments['guard'] if arguments['guard'] != NULL_ADDRESS else None
            if safe_status.guard:
                logger.debug('Setting Guard')
            else:
                logger.debug('Unsetting Guard')
            self.store_new_safe_status(safe_status, internal_tx)
        elif function_name == 'enableModule':
            logger.debug('Enabling Module')
            safe_status = self.get_last_safe_status_for_address(contract_address)
            safe_status.enabled_modules.append(arguments['module'])
            self.store_new_safe_status(safe_status, internal_tx)
        elif function_name == 'disableModule':
            logger.debug('Disabling Module')
            safe_status = self.get_last_safe_status_for_address(contract_address)
            safe_status.enabled_modules.remove(arguments['module'])
            self.store_new_safe_status(safe_status, internal_tx)
        elif function_name in {'execTransactionFromModule', 'execTransactionFromModuleReturnData'}:
            logger.debug('Executing Tx from Module')
            # TODO Add test with previous traces for processing a module transaction
            ethereum_tx = internal_tx.ethereum_tx
            if 'module' in arguments:  # L2 Safe with event SafeModuleTransaction
                module_address = arguments['module']
            else:
                # Someone calls Module -> Module calls Safe Proxy -> Safe Proxy delegate calls Master Copy
                # The trace that is been processed is the last one, so indexer needs to go at least 2 traces back
                previous_trace = self.ethereum_client.parity.get_previous_trace(internal_tx.ethereum_tx_id,
                                                                                internal_tx.trace_address_as_list,
                                                                                number_traces=2,
                                                                                skip_delegate_calls=True)
                if not previous_trace:
                    message = f'Cannot find previous trace for tx-hash={HexBytes(internal_tx.ethereum_tx_id).hex()} ' \
                              f'and trace-address={internal_tx.trace_address}'
                    logger.warning(message)
                    raise ValueError(message)
                module_internal_tx = InternalTx.objects.build_from_trace(previous_trace, internal_tx.ethereum_tx)
                module_address = module_internal_tx.to if module_internal_tx else NULL_ADDRESS
            module_data = HexBytes(arguments['data'])
            failed = self.is_module_failed(ethereum_tx, module_address, contract_address)
            ModuleTransaction.objects.get_or_create(
                internal_tx=internal_tx,
                defaults={
                    'created': internal_tx.ethereum_tx.block.timestamp,
                    'safe': contract_address,
                    'module': module_address,
                    'to': arguments['to'],
                    'value': arguments['value'],
                    'data': module_data if module_data else None,
                    'operation': arguments['operation'],
                    'failed': failed,
                }
            )

        elif function_name == 'approveHash':
            logger.debug('Processing hash approval')
            multisig_transaction_hash = arguments['hashToApprove']
            ethereum_tx = internal_tx.ethereum_tx
            if 'owner' in arguments:  # Event approveHash
                owner = arguments['owner']
            else:
                previous_trace = self.ethereum_client.parity.get_previous_trace(internal_tx.ethereum_tx_id,
                                                                                internal_tx.trace_address_as_list,
                                                                                skip_delegate_calls=True)
                if not previous_trace:
                    message = f'Cannot find previous trace for tx-hash={HexBytes(internal_tx.ethereum_tx_id).hex()} and ' \
                              f'trace-address={internal_tx.trace_address}'
                    logger.warning(message)
                    raise ValueError(message)
                previous_internal_tx = InternalTx.objects.build_from_trace(previous_trace, internal_tx.ethereum_tx)
                owner = previous_internal_tx._from
            safe_signature = SafeSignatureApprovedHash.build_for_owner(owner, multisig_transaction_hash)
            (multisig_confirmation,
             _) = MultisigConfirmation.objects.get_or_create(multisig_transaction_hash=multisig_transaction_hash,
                                                             owner=owner,
                                                             defaults={
                                                                 'created': internal_tx.ethereum_tx.block.timestamp,
                                                                 'ethereum_tx': ethereum_tx,
                                                                 'signature': safe_signature.export_signature(),
                                                                 'signature_type': safe_signature.signature_type.value,
                                                             })
            if not multisig_confirmation.ethereum_tx_id:
                multisig_confirmation.ethereum_tx = ethereum_tx
                multisig_confirmation.save(update_fields=['ethereum_tx'])
        elif function_name == 'execTransaction':
            logger.debug('Processing transaction execution')
            safe_status = self.get_last_safe_status_for_address(contract_address)
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
            # MultisigTransaction.objects.filter(
            #    ethereum_tx=ethereum_tx,
            #    nonce=safe_tx.safe_nonce,
            #    safe=contract_address
            # ).exclude(
            #     safe_tx_hash=safe_tx_hash
            # ).delete()

            # Remove old txs not used
            # MultisigTransaction.objects.filter(
            #     ethereum_tx=None,
            #     nonce__lt=safe_tx.safe_nonce,
            #     safe=contract_address
            # ).delete()

            failed = self.is_failed(ethereum_tx, safe_tx_hash)
            multisig_tx, _ = MultisigTransaction.objects.get_or_create(
                safe_tx_hash=safe_tx_hash,
                defaults={
                    'created': internal_tx.ethereum_tx.block.timestamp,
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
                    'trusted': True,
                })
            if not multisig_tx.ethereum_tx_id:
                multisig_tx.ethereum_tx = ethereum_tx
                multisig_tx.failed = failed
                multisig_tx.signatures = HexBytes(arguments['signatures'])
                multisig_tx.trusted = True
                multisig_tx.save(update_fields=['ethereum_tx', 'failed', 'signatures', 'trusted'])

            for safe_signature in SafeSignature.parse_signature(safe_tx.signatures, safe_tx_hash):
                multisig_confirmation, _ = MultisigConfirmation.objects.get_or_create(
                    multisig_transaction_hash=safe_tx_hash,
                    owner=safe_signature.owner,
                    defaults={
                        'created': internal_tx.ethereum_tx.block.timestamp,
                        'ethereum_tx': None,
                        'multisig_transaction': multisig_tx,
                        'signature': safe_signature.export_signature(),
                        'signature_type': safe_signature.signature_type.value,
                    }
                )
                if multisig_confirmation.signature != safe_signature.signature:
                    multisig_confirmation.signature = safe_signature.export_signature()
                    multisig_confirmation.signature_type = safe_signature.signature_type.value
                    multisig_confirmation.save(update_fields=['signature', 'signature_type'])

            safe_status.nonce = nonce + 1
            self.store_new_safe_status(safe_status, internal_tx)
        elif function_name == 'execTransactionFromModule':
            logger.debug('Not processing execTransactionFromModule')
            # No side effects or nonce increasing, but trace will be set as processed
        else:
            processed_successfully = False
        logger.debug('End processing')
        return processed_successfully
