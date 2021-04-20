import operator
from collections import OrderedDict
from logging import getLogger
from typing import Any, Dict, Iterable, List, Optional, Sequence

from cache_memoize import cache_memoize
from cachetools import cachedmethod
from eth_abi.exceptions import DecodingError
from requests import RequestException
from web3.exceptions import BadFunctionCallOutput

from gnosis.eth import EthereumClient

from ..models import EthereumEvent, SafeContract
from .ethereum_indexer import EthereumIndexer

logger = getLogger(__name__)


class Erc20EventsIndexerProvider:
    def __new__(cls):
        if not hasattr(cls, 'instance'):
            from django.conf import settings
            cls.instance = Erc20EventsIndexer(EthereumClient(settings.ETHEREUM_NODE_URL))
        return cls.instance

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, 'instance'):
            del cls.instance


class Erc20EventsIndexer(EthereumIndexer):
    """
    Indexes ERC20 and ERC721 `Transfer` Event (as ERC721 has the same topic)
    """

    def __init__(self, ethereum_client: EthereumClient,
                 block_process_limit: int = 10000,
                 updated_blocks_behind: int = 300,  # For last 300 blocks, process `query_chunk_size` Safes together
                 query_chunk_size: int = 500,
                 *args, **kwargs):
        super().__init__(ethereum_client,
                         block_process_limit=block_process_limit,
                         updated_blocks_behind=updated_blocks_behind,
                         query_chunk_size=query_chunk_size,
                         *args, **kwargs)
        self._cache_is_erc20 = {}

    @property
    def database_model(self):
        return SafeContract

    @property
    def database_field(self):
        return 'erc20_block_number'

    def find_relevant_elements(self, addresses: List[str], from_block_number: int,
                               to_block_number: int,
                               current_block_number: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Search for tx hashes with erc20 transfer events (`from` and `to`) of a `safe_address`
        :param addresses:
        :param from_block_number: Starting block number
        :param to_block_number: Ending block number
        :param current_block_number: Current block number (for cache purposes)
        :return: Tx hashes of txs with relevant erc20 transfer events for the `addresses`
        """
        addresses_len = len(addresses)

        # Disable find elements without transfer topics. It's too much for the nodes
        # if (current_block_number - self.updated_blocks_behind) < from_block_number:
        #    logger.info('Searching for all erc20/721 events from block-number=%d to block-number=%d - '
        #                'Number of Safes=%d', from_block_number, to_block_number, addresses_len)
        #    erc20_transfer_events = self._find_elements_without_transfer_topics(addresses, from_block_number,
        #                                                                        to_block_number)
        # else:
        logger.debug('Filtering for erc20/721 events from block-number=%d to block-number=%d - '
                     'Number of Safes=%d', from_block_number, to_block_number, addresses_len)
        erc20_transfer_events = self._find_elements_using_transfer_topics(addresses, from_block_number, to_block_number)

        len_erc20_transfer_events = len(erc20_transfer_events)
        logger_fn = logger.info if len_erc20_transfer_events else logger.debug
        logger_fn('Found %d erc20/721 events between block-number=%d and block-number=%d. Number of Safes=%d',
                  len_erc20_transfer_events, from_block_number, to_block_number, addresses_len)

        return erc20_transfer_events

    def _find_elements_using_transfer_topics(self, addresses: Sequence[str], from_block_number: int,
                                             to_block_number: int):
        """
        It will get ERC20/721 using topics for filtering. Some transactions without topics will be missed, but
        that's the only way to sync the events in a reasonable amount of time.
        :param addresses:
        :param from_block_number:
        :param to_block_number:
        :return: List of events
        """
        try:
            return self.ethereum_client.erc20.get_total_transfer_history(addresses,
                                                                         from_block=from_block_number,
                                                                         to_block=to_block_number)
        except RequestException as e:
            raise self.FindRelevantElementsException('Request error retrieving erc20 events') from e

    def _find_elements_without_transfer_topics(self, addresses: Sequence[str], from_block_number: int,
                                               to_block_number: int) -> List[Dict[str, Any]]:
        """
        It will get all ERC20/721 events for EVERY ethereum address to be filtered afterwards, due to some Transfer
        events not having `from` or `to` as topics, so they cannot be queried.
        :param addresses:
        :param from_block_number:
        :param to_block_number:
        :return: List of events
        """
        try:
            erc20_transfer_events = self.ethereum_client.erc20.get_total_transfer_history(from_block=from_block_number,
                                                                                          to_block=to_block_number)
        except RequestException as e:
            raise self.FindRelevantElementsException('Request error retrieving erc20 events') from e

        filtered_events = []
        addresses_set = set(addresses)  # Linear time `in` filtering
        for event in erc20_transfer_events:
            event_args = event.get('args')
            if event_args and (event_args.get('from') in addresses_set or event_args.get('to') in addresses_set):
                filtered_events.append(self._transform_transfer_event(event))
        return filtered_events

    def _transform_transfer_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """
        :param event: Be careful, it will be modified instead of copied
        :return: The same event if it's a ERC20/ERC721. Tries to tell apart if it's not defined (`unknown` instead
        of `value` or `tokenId`)
        """
        event_args = event['args']
        if 'unknown' in event_args:  # Not standard event, trying to tell apart ERC20 from ERC721
            logger.info('Cannot tell apart erc20 or 721 for token-address=%s - Checking token decimals',
                        event['address'])
            value = event_args['unknown']
            del event_args['unknown']
            if self._is_erc20(event['address']):
                event_args['value'] = value
            else:
                event_args['tokenId'] = value
        return event

    @cachedmethod(cache=operator.attrgetter('_cache_is_erc20'))
    @cache_memoize(60 * 60 * 24, prefix='erc20-events-indexer-is-erc20')  # 1 day
    def _is_erc20(self, token_address: str) -> bool:
        try:
            decimals = self.ethereum_client.erc20.get_decimals(token_address)
            return decimals >= 0
        except (ValueError, BadFunctionCallOutput, DecodingError):
            return False

    def process_elements(self, events: Iterable[Dict[str, Any]]) -> List[EthereumEvent]:
        """
        Process all events found by `find_relevant_elements`
        :param events: Events to store in database
        :return: List of `EthereumEvent` already stored in database
        """
        tx_hashes = list(OrderedDict.fromkeys([event['transactionHash'] for event in events]))
        ethereum_txs = self.index_service.txs_create_or_update_from_tx_hashes(tx_hashes)  # noqa: F841
        ethereum_events = [EthereumEvent.objects.from_decoded_event(event) for event in events]
        return EthereumEvent.objects.bulk_create(ethereum_events, ignore_conflicts=True)
