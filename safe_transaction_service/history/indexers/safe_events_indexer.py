import operator
from collections import OrderedDict
from functools import cached_property
from logging import getLogger
from typing import Any, Dict, Iterable, List, Optional, Sequence

from cache_memoize import cache_memoize
from cachetools import cachedmethod
from eth_abi.exceptions import DecodingError
from eth_utils import event_abi_to_log_topic
from requests import RequestException
from web3.contract import ContractEvent
from web3.exceptions import BadFunctionCallOutput
from web3.types import EventData, FilterParams, LogReceipt

from gnosis.eth import EthereumClient
from gnosis.eth.contracts import get_safe_V1_3_0_contract

from ..models import EthereumEvent, SafeContract
from .ethereum_indexer import EthereumIndexer

logger = getLogger(__name__)


class SafeEventsIndexerProvider:
    def __new__(cls):
        if not hasattr(cls, 'instance'):
            from django.conf import settings
            cls.instance = SafeEventsIndexer(EthereumClient(settings.ETHEREUM_NODE_URL))
        return cls.instance

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, 'instance'):
            del cls.instance


class SafeEventsIndexer(EthereumIndexer):
    """
    Indexes ERC20 and ERC721 `Transfer` Event (as ERC721 has the same topic)
    """

    @cached_property
    def events_to_listen(self) -> Dict[bytes, ContractEvent]:
        """
        event SafeMultiSigTransaction(
            address to,
            uint256 value,
            bytes data,
            Enum.Operation operation,
            uint256 safeTxGas,
            uint256 baseGas,
            uint256 gasPrice,
            address gasToken,
            address payable refundReceiver,
            bytes signatures,
            // We combine nonce, sender and threshold into one to avoid stack too deep
            // Dev note: additionalInfo should not contain `bytes`, as this complicates decoding
            bytes additionalInfo
        );

        event SafeModuleTransaction(
            address module,
            address to,
            uint256 value,
            bytes data,
            Enum.Operation operation,
            bool success
        );

        event SafeSetup(
            address indexed initiator,
            address[] owners,
            uint256 threshold,
            address initializer,
            address fallbackHandler
        );

        event ApproveHash(
            bytes32 indexed approvedHash,
            address indexed owner
        );

        event SignMsg(
            bytes32 indexed msgHash
        );

        event ExecutionFailure(
            bytes32 txHash, uint256 payment
        );

        event ExecutionSuccess(
            bytes32 txHash, uint256 payment
        );

        event EnabledModule(address module);
        event DisabledModule(address module);
        event ExecutionFromModuleSuccess(address indexed module);
        event ExecutionFromModuleFailure(address indexed module);

        event AddedOwner(address owner);
        event RemovedOwner(address owner);
        event ChangedThreshold(uint256 threshold);

        event SafeReceived(address indexed sender, uint256 value);  // Incoming ether

        :return:
        """
        safe_contract = get_safe_V1_3_0_contract(self.ethereum_client.w3)
        events = [
            safe_contract.events.SafeMultiSigTransaction(),
            safe_contract.events.SafeModuleTransaction(),
            safe_contract.events.SafeSetup(),
            safe_contract.events.ApproveHash(),
            safe_contract.events.SignMsg(),
            safe_contract.events.ExecutionFailure(),
            safe_contract.events.ExecutionSuccess(),
            # Modules
            safe_contract.events.EnabledModule(),
            safe_contract.events.DisabledModule(),
            safe_contract.events.ExecutionFromModuleSuccess(),
            safe_contract.events.ExecutionFromModuleFailure(),
            # Owners
            safe_contract.events.AddedOwner(),
            safe_contract.events.RemovedOwner(),
            safe_contract.events.ChangedThreshold(),
            # Incoming Ether
            safe_contract.events.SafeReceived(),
        ]
        return {event_abi_to_log_topic(event.abi): event for event in events}

    @property
    def database_model(self):
        return SafeContract

    @property
    def database_field(self):
        return 'erc20_block_number'

    def find_relevant_elements(self, addresses: List[str], from_block_number: int,
                               to_block_number: int,
                               current_block_number: Optional[int] = None) -> List[LogReceipt]:
        """
        Search for log receipts for Safe events
        :param addresses: Not used
        :param from_block_number: Starting block number
        :param to_block_number: Ending block number
        :param current_block_number: Current block number (for cache purposes)
        :return: LogReceipt for matching events
        """
        logger.debug('Filtering for Safe events from block-number=%d to block-number=%d', from_block_number,
                     to_block_number)
        log_receipts = self._find_elements_using_topics(from_block_number, to_block_number)

        len_events = len(log_receipts)
        logger_fn = logger.info if len_events else logger.debug
        logger_fn('Found %d Safe events between block-number=%d and block-number=%d',
                  len_events, from_block_number, to_block_number)
        return log_receipts

    def _find_elements_using_topics(self, from_block_number: int, to_block_number: int) -> List[LogReceipt]:
        """
        It will get Safe events using all the Gnosis Safe topics for filtering.
        :param from_block_number:
        :param to_block_number:
        :return: LogReceipt for matching events
        """
        filter_topics = list(self.events_to_listen.keys())
        parameters: FilterParams = {
            'fromBlock': from_block_number,
            'toBlock': to_block_number,
            'topics': [filter_topics]
        }

        try:
            return self.ethereum_client.slow_w3.eth.get_logs(parameters)
        except IOError as e:
            raise self.FindRelevantElementsException('Request error retrieving Safe L2 events') from e

    def process_elements(self, log_receipts: Sequence[LogReceipt]) -> int:
        """
        Process all events found by `find_relevant_elements`
        :param log_receipts: Events to store in database
        :return: List of `EthereumEvent` already stored in database
        """
        decoded_elements: List[EventData] = [self.events_to_listen[log_receipt['topic']].processLog(log_receipt)
                                             for log_receipt in log_receipts]
        # TODO Emulate InternalTx and InternalTxDecoded
        return 0  # TODO Number of InternalTxDecoded created
