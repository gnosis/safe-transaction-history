from collections import OrderedDict
from logging import getLogger
from typing import Generator, List, Optional, Sequence, Set

from django.db import transaction

from requests import RequestException

from gnosis.eth import EthereumClient

from safe_transaction_service.contracts.tx_decoder import (CannotDecode,
                                                           get_safe_tx_decoder)

from ..models import (InternalTx, InternalTxDecoded, MonitoredAddress,
                      SafeMasterCopy)
from .ethereum_indexer import EthereumIndexer

logger = getLogger(__name__)


class InternalTxIndexerProvider:
    def __new__(cls):
        if not hasattr(cls, 'instance'):
            from django.conf import settings
            block_process_limit = settings.ETH_INTERNAL_TXS_BLOCK_PROCESS_LIMIT
            if settings.ETH_INTERNAL_NO_FILTER:
                cls.instance = InternalTxIndexerWithTraceBlock(
                    EthereumClient(settings.ETHEREUM_TRACING_NODE_URL),
                    block_process_limit=min(block_process_limit, 500)
                )
            else:
                cls.instance = InternalTxIndexer(
                    EthereumClient(settings.ETHEREUM_TRACING_NODE_URL),
                    block_process_limit=block_process_limit
                )
        return cls.instance

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, 'instance'):
            del cls.instance


class InternalTxIndexer(EthereumIndexer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tx_decoder = get_safe_tx_decoder()
        self.number_trace_blocks = 10  # Use `trace_block` for last `number_trace_blocks` blocks indexing

    @property
    def database_field(self):
        return 'tx_block_number'

    @property
    def database_model(self):
        return SafeMasterCopy

    def find_relevant_elements(self, addresses: Sequence[str], from_block_number: int,
                               to_block_number: int, current_block_number: Optional[int] = None) -> Set[str]:
        current_block_number = current_block_number or self.ethereum_client.current_block_number
        # Use `trace_block` for last `number_trace_blocks` blocks and `trace_filter` for the others
        trace_block_number = max(current_block_number - self.number_trace_blocks, 0)
        if from_block_number > trace_block_number:  # Just trace_block
            logger.debug('Using trace_block from-block=%d to-block=%d', from_block_number, to_block_number)
            return self._find_relevant_elements_using_trace_block(addresses, from_block_number, to_block_number)
        elif to_block_number < trace_block_number:  # Just trace_filter
            logger.debug('Using trace_filter from-block=%d to-block=%d', from_block_number, to_block_number)
            return self._find_relevant_elements_using_trace_filter(addresses, from_block_number, to_block_number)
        else:  # trace_filter for old blocks and trace_filter for the most recent ones
            logger.debug('Using trace_filter from-block=%d to-block=%d and trace_block from-block=%d to-block=%d',
                         from_block_number, trace_block_number, trace_block_number, to_block_number)
            return OrderedDict.fromkeys(list(self._find_relevant_elements_using_trace_filter(addresses,
                                                                                             from_block_number,
                                                                                             trace_block_number))
                                        + list(self._find_relevant_elements_using_trace_block(addresses,
                                                                                              trace_block_number,
                                                                                              to_block_number))
                                        ).keys()

    def _find_relevant_elements_using_trace_block(self, addresses: Sequence[str], from_block_number: int,
                                                  to_block_number: int) -> Set[str]:
        addresses_set = set(addresses)  # More optimal to use with `in`
        try:
            block_numbers = list(range(from_block_number, to_block_number + 1))
            traces = self.ethereum_client.parity.trace_blocks(block_numbers)
            tx_hashes = []
            for block_number, trace_list in zip(block_numbers, traces):
                if not trace_list:
                    logger.warning('Empty `trace_block` for block=%d', block_number)
                tx_hashes.extend([trace['transactionHash'] for trace in trace_list
                                  if trace.get('action', {}).get('from') in addresses_set
                                  or trace.get('action', {}).get('to') in addresses_set])
            return OrderedDict.fromkeys(tx_hashes).keys()
        except RequestException as e:
            raise self.FindRelevantElementsException('Request error calling `trace_block`') from e

    def _find_relevant_elements_using_trace_filter(self, addresses: Sequence[str], from_block_number: int,
                                                   to_block_number: int) -> Set[str]:
        """
        Search for tx hashes with internal txs (in and out) of a `address`
        :param addresses:
        :param from_block_number: Starting block number
        :param to_block_number: Ending block number
        :return: Tx hashes of txs with internal txs relevant for the `addresses`
        """
        logger.debug('Searching for internal txs from block-number=%d to block-number=%d - Addresses=%s',
                     from_block_number, to_block_number, addresses)

        try:
            to_traces = self.ethereum_client.parity.trace_filter(from_block=from_block_number,
                                                                 to_block=to_block_number,
                                                                 to_address=addresses)

            from_traces = self.ethereum_client.parity.trace_filter(from_block=from_block_number,
                                                                   to_block=to_block_number,
                                                                   from_address=addresses)
        except RequestException as e:
            raise self.FindRelevantElementsException('Request error calling `trace_filter`') from e

        # Log INFO if traces found, DEBUG if not
        tx_hashes = OrderedDict.fromkeys([trace['transactionHash']
                                          for trace in (to_traces + from_traces)]).keys()
        log_fn = logger.info if len(tx_hashes) else logger.debug
        log_fn('Found %d relevant txs with internal txs between block-number=%d and block-number=%d. Addresses=%s',
               len(tx_hashes), from_block_number, to_block_number, addresses)

        return tx_hashes

    def _get_internal_txs_to_decode(self, tx_hashes: Sequence[str]) -> Generator[InternalTxDecoded, None, None]:
        """
        Use generator to be more RAM friendly
        """
        for internal_tx in InternalTx.objects.can_be_decoded().filter(ethereum_tx__in=tx_hashes):
            try:
                function_name, arguments = self.tx_decoder.decode_transaction(bytes(internal_tx.data))
                if internal_tx.pk is None:  # pk is not populated on `bulk_create ignore_conflicts=True`
                    internal_tx = InternalTx.objects.get(ethereum_tx=internal_tx.ethereum_tx,
                                                         trace_address=internal_tx.trace_address)
                yield InternalTxDecoded(internal_tx=internal_tx,
                                        function_name=function_name,
                                        arguments=arguments,
                                        processed=False)
            except CannotDecode:
                pass

    def process_elements(self, tx_hashes: Sequence[str]) -> List[InternalTx]:
        # Prefetch ethereum txs
        if not tx_hashes:
            return []

        logger.debug('Prefetching and storing %d ethereum txs', len(tx_hashes))
        ethereum_txs = self.index_service.txs_create_or_update_from_tx_hashes(tx_hashes)
        logger.debug('End prefetching and storing of ethereum txs')

        logger.debug('Prefetching of traces(internal txs)')
        internal_txs = (InternalTx.objects.build_from_trace(trace, ethereum_tx)
                        for ethereum_tx, traces in zip(ethereum_txs,
                                                       self.ethereum_client.parity.trace_transactions(tx_hashes))
                        for trace in self.ethereum_client.parity.filter_out_errored_traces(traces))
        revelant_internal_txs_batch = (trace for trace in internal_txs if trace.is_relevant)
        logger.debug('End prefetching of traces(internal txs)')

        logger.debug('Storing traces')
        with transaction.atomic():
            traces_stored = InternalTx.objects.bulk_create_from_generator(
                revelant_internal_txs_batch, ignore_conflicts=True
            )
            logger.debug('End storing of %d traces', traces_stored)

            logger.debug('Start decoding and storing of decoded traces')
            internal_txs_decoded = InternalTxDecoded.objects.bulk_create_from_generator(
                self._get_internal_txs_to_decode(tx_hashes), ignore_conflicts=True
            )
            logger.debug('End decoding and storing of %d decoded traces', internal_txs_decoded)
            return tx_hashes


class InternalTxIndexerWithTraceBlock(InternalTxIndexer):
    """
    Indexer for nodes not supporting `trace_filter`, so it will always use `trace_block`
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def get_almost_updated_addresses(self, current_block_number: int) -> List[MonitoredAddress]:
        """
        Return every address. As we are using `trace_block` every master copy should be processed together
        :param current_block_number:
        :return:
        """
        return self.get_not_updated_addresses(current_block_number)

    def find_relevant_elements(self, addresses: Sequence[str], from_block_number: int,
                               to_block_number: int, current_block_number: Optional[int] = None) -> Set[str]:
        logger.debug('Using trace_block from-block=%d to-block=%d', from_block_number, to_block_number)
        return self._find_relevant_elements_using_trace_block(addresses, from_block_number, to_block_number)
