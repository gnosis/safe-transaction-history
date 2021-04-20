import copyreg
import logging
import pickle
from collections import defaultdict
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from django.db.models import (Case, F, OuterRef, Q, QuerySet, Subquery, Value,
                              When)
from django.utils import timezone

from redis import Redis

from gnosis.eth import EthereumClient, EthereumClientProvider
from gnosis.eth.django.models import Uint256Field

from safe_transaction_service.tokens.models import Token

from ..models import (EthereumEvent, EthereumTx, EthereumTxCallType,
                      InternalTx, ModuleTransaction, MultisigTransaction,
                      TransferDict)
from ..serializers import (
    EthereumTxWithTransfersResponseSerializer,
    SafeModuleTransactionWithTransfersResponseSerializer,
    SafeMultisigTransactionWithTransfersResponseSerializer)
from ..utils import get_redis

logger = logging.getLogger(__name__)


class TransactionServiceException(Exception):
    pass


class TransactionServiceProvider:
    def __new__(cls):
        if not hasattr(cls, 'instance'):
            cls.instance = TransactionService(EthereumClientProvider(), get_redis())
        return cls.instance

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, 'instance'):
            del cls.instance


class TransactionService:
    def __init__(self, ethereum_client: EthereumClient, redis: Redis):
        self.ethereum_client = ethereum_client
        self.redis = redis
        # Encode memoryview for redis
        copyreg.pickle(memoryview, lambda val: (memoryview, (bytes(val),)))

    #  Cache methods ---------------------------------
    def get_cache_key(self, safe_address: str, tx_hash: str):
        return f'tx-service:{safe_address}:{tx_hash}'

    def get_txs_from_cache(self, safe_address: str,
                           hashes_to_search: Sequence[str]
                           ) -> List[Union[EthereumTx, MultisigTransaction, ModuleTransaction]]:
        keys_to_search = [self.get_cache_key(safe_address, hash_to_search) for hash_to_search in hashes_to_search]
        return [pickle.loads(data) if data else None for data in self.redis.mget(keys_to_search)]

    def store_txs_in_cache(self, safe_address: str,
                           hashes_with_txs: Tuple[str, Union[EthereumTx, MultisigTransaction, ModuleTransaction]]):
        """
        Store executed transactions older than 10 minutes, using `ethereum_tx_hash` as key (for
        MultisigTransaction it will be `SafeTxHash`) and expire then in one hour
        :param safe_address:
        :param hashes_with_txs:
        """
        # Just store executed transactions older than 10 minutes
        to_store = {self.get_cache_key(safe_address, tx_hash): pickle.dumps(tx) for tx_hash, tx in hashes_with_txs
                    if tx.execution_date and (tx.execution_date + timedelta(minutes=10)) < timezone.now()}
        if to_store:
            pipe = self.redis.pipeline()
            pipe.mset(to_store)
            for key in to_store.keys():
                pipe.expire(key, 60 * 60)  # Expire in one hour
            pipe.execute()
    # End of cache methods ----------------------------

    def get_all_tx_hashes(self, safe_address: str, executed: bool = False,
                          queued: bool = True, trusted: bool = True) -> QuerySet:
        """
        Build a queryset with hashes for every tx for a Safe for pagination filtering. In the case of
        Multisig Transactions, as some of them are not mined, we use the SafeTxHash
        Criteria for building this list:
          - Return only multisig txs with `nonce < current Safe Nonce`
          - The endpoint should only show incoming transactions that have been mined
          - The transactions should be sorted by execution date. If an outgoing transaction doesn't have an execution
          date the execution date of the transaction with the same nonce that has been executed should be taken.
          - Incoming and outgoing transfers or Eth/tokens must be under a multisig/module tx if triggered by one.
          Otherwise they should have their own entry in the list using a EthereumTx
        :param safe_address:
        :param executed: By default `False`, all transactions are returned. With `True`, just txs executed are returned.
        :param queued: By default `True`, all transactions are returned. With `False`, just txs with
        `nonce < current Safe Nonce` are returned.
        :param trusted: By default `True`, just txs that are trusted are returned (with at least one confirmation,
        sent by a delegate or indexed). With `False` all txs are returned
        :return: List with tx hashes sorted by date (newest first)
        """

        # If tx is not mined, get the execution date of a tx mined with the same nonce
        case = Case(
            When(ethereum_tx__block=None,
                 then=MultisigTransaction.objects.filter(
                     safe=OuterRef('safe'),
                     nonce=OuterRef('nonce')
                 ).exclude(
                     ethereum_tx__block=None
                 ).values('ethereum_tx__block__timestamp')),
            default=F('ethereum_tx__block__timestamp')
        )
        multisig_safe_tx_ids = MultisigTransaction.objects.filter(
            safe=safe_address
        ).annotate(
            execution_date=case,
            block=F('ethereum_tx__block_id'),
            safe_nonce=F('nonce'),
        ).values('safe_tx_hash', 'execution_date',
                 'created', 'block', 'safe_nonce')  # Tricky, we will merge SafeTx hashes with EthereumTx hashes
        # Block is needed to get stable ordering

        if not queued:  # Filter out txs with nonce >= Safe nonce
            last_nonce_query = MultisigTransaction.objects.filter(
                safe=safe_address
            ).exclude(ethereum_tx=None).order_by('-nonce').values('nonce')
            multisig_safe_tx_ids = multisig_safe_tx_ids.filter(nonce__lte=Subquery(last_nonce_query[:1]))

        if trusted:  # Just show trusted transactions
            multisig_safe_tx_ids = multisig_safe_tx_ids.filter(trusted=True)

        if executed:
            multisig_safe_tx_ids = multisig_safe_tx_ids.exclude(ethereum_tx__block=None)

        # Get module txs
        module_tx_ids = ModuleTransaction.objects.filter(
            safe=safe_address
        ).annotate(
            execution_date=F('internal_tx__ethereum_tx__block__timestamp'),
            block=F('internal_tx__ethereum_tx__block_id'),
            safe_nonce=Value(0, output_field=Uint256Field()),
        ).distinct().values('internal_tx__ethereum_tx_id', 'execution_date', 'created', 'block', 'safe_nonce')

        multisig_hashes = MultisigTransaction.objects.filter(safe=safe_address
                                                             ).exclude(ethereum_tx=None).values('ethereum_tx_id')
        module_hashes = ModuleTransaction.objects.filter(safe=safe_address).values('internal_tx__ethereum_tx_id')
        multisig_and_module_hashes = multisig_hashes.union(module_hashes)

        # Get incoming/outgoing tokens not included on Multisig or Module txs.
        # Outgoing tokens can be triggered by another user after the Safe calls `approve`, that's why it will not
        # always appear as a MultisigTransaction
        event_tx_ids = EthereumEvent.objects.erc20_and_721_events().filter(
            Q(arguments__to=safe_address) | Q(arguments__from=safe_address)
        ).exclude(
            ethereum_tx__in=multisig_and_module_hashes
        ).annotate(
            execution_date=F('ethereum_tx__block__timestamp'),
            created=F('ethereum_tx__block__timestamp'),
            block=F('ethereum_tx__block_id'),
            safe_nonce=Value(0, output_field=Uint256Field()),
        ).distinct().values('ethereum_tx_id', 'execution_date', 'created', 'block', 'safe_nonce')

        # Get incoming txs not included on Multisig or Module txs
        internal_tx_ids = InternalTx.objects.filter(
            call_type=EthereumTxCallType.CALL.value,
            value__gt=0,
            to=safe_address,
        ).exclude(
            ethereum_tx__in=multisig_and_module_hashes
        ).annotate(
            execution_date=F('ethereum_tx__block__timestamp'),
            created=F('ethereum_tx__block__timestamp'),
            block=F('ethereum_tx__block_id'),
            safe_nonce=Value(0, output_field=Uint256Field()),
        ).distinct().values('ethereum_tx_id', 'execution_date', 'created', 'block', 'safe_nonce')

        # Tricky, we merge SafeTx hashes with EthereumTx hashes
        queryset = multisig_safe_tx_ids.distinct().union(
            event_tx_ids
        ).union(
            internal_tx_ids
        ).union(
            module_tx_ids
        ).order_by('-execution_date', '-safe_nonce', 'block', '-created')
        # Order by block because `block_number < NULL`, so txs mined will have preference,
        # and `created` to get always the same ordering with not executed transactions, as they will share
        # the same `execution_date` that the mined tx
        return queryset

    def get_all_txs_from_hashes(self, safe_address: str,
                                hashes_to_search: Sequence[str]) -> List[Union[EthereumTx,
                                                                               MultisigTransaction,
                                                                               ModuleTransaction]]:
        """
        Now that we know how to paginate, we retrieve the real transactions
        :param safe_address:
        :param hashes_to_search:
        :return:
        """
        cached_txs = {hash_to_search: cached_tx
                      for hash_to_search, cached_tx
                      in zip(hashes_to_search, self.get_txs_from_cache(safe_address, hashes_to_search))
                      if cached_tx}
        hashes_not_cached = [hash_to_search for hash_to_search in hashes_to_search if hash_to_search not in cached_txs]
        multisig_txs = {multisig_tx.safe_tx_hash: multisig_tx
                        for multisig_tx in
                        MultisigTransaction.objects.filter(
                            safe=safe_address,
                            safe_tx_hash__in=hashes_not_cached
                        ).with_confirmations_required(
                        ).prefetch_related(
                            'confirmations'
                        ).select_related(
                            'ethereum_tx__block'
                        ).order_by(
                            '-nonce',
                            '-created'
                        )}

        module_txs = {module_tx.internal_tx.ethereum_tx_id: module_tx
                      for module_tx in
                      ModuleTransaction.objects.filter(
                          safe=safe_address,
                          internal_tx__ethereum_tx__in=hashes_not_cached
                      ).select_related('internal_tx')}

        plain_ethereum_txs = {ethereum_tx.tx_hash: ethereum_tx
                              for ethereum_tx in EthereumTx.objects.filter(tx_hash__in=hashes_not_cached
                                                                           ).select_related('block')}

        # We also need the in/out transfers for the MultisigTxs
        all_hashes = hashes_not_cached + [multisig_tx.ethereum_tx_id for multisig_tx in multisig_txs.values()]

        tokens_queryset = InternalTx.objects.token_txs_for_address(safe_address).filter(
            ethereum_tx__in=all_hashes)
        ether_queryset = InternalTx.objects.ether_txs_for_address(safe_address).filter(
            ethereum_tx__in=all_hashes)

        # Build dict of transfers for optimizing access
        transfer_dict = defaultdict(list)
        transfers: List[TransferDict] = InternalTx.objects.union_ether_and_token_txs(tokens_queryset,
                                                                                     ether_queryset)
        for transfer in transfers:
            transfer_dict[transfer['transaction_hash']].append(transfer)

        # Add available information about the token on database for the transfers
        tokens = {token.address: token
                  for token in Token.objects.filter(address__in={transfer['token_address'] for transfer in transfers
                                                                 if transfer['token_address']})}
        for transfer in transfers:
            transfer['token'] = tokens.get(transfer['token_address'])

        # Build the list
        def get_the_transaction(transaction_id: str) -> Optional[Union[MultisigTransaction,
                                                                       ModuleTransaction,
                                                                       EthereumTx]]:
            if result := cached_txs.get(transaction_id):
                return result

            multisig_tx: MultisigTransaction
            module_tx: ModuleTransaction
            plain_ethereum_tx: EthereumTx
            result: Optional[Union[MultisigTransaction, ModuleTransaction, EthereumTx]]

            if result := multisig_txs.get(transaction_id):
                # Populate transfers
                result.transfers = transfer_dict[result.ethereum_tx_id]
                return result

            if result := module_txs.get(transaction_id):
                result.transfers = transfer_dict[result.internal_tx.ethereum_tx_id]
                return result

            if result := plain_ethereum_txs.get(transaction_id):
                # If no Multisig or Module tx found, fallback to simple tx
                result.transfers = transfer_dict[result.tx_hash]
                return result

            # This cannot happen if logic is ok
            if not result:
                raise ValueError('Tx not found, problem merging all transactions together')

        hash_with_txs = [(hash_to_search, get_the_transaction(hash_to_search))
                         for hash_to_search in hashes_to_search]
        self.store_txs_in_cache(safe_address, hash_with_txs)
        return list(dict.fromkeys(tx for _, tx in hash_with_txs))  # Sorted already by execution_date

    def serialize_all_txs(self, models: List[Union[EthereumTx,
                                                   MultisigTransaction,
                                                   ModuleTransaction]]) -> List[Dict[str, Any]]:
        results = []
        for model in models:
            model_type = type(model)
            if model_type == EthereumTx:
                serializer = EthereumTxWithTransfersResponseSerializer
            elif model_type == ModuleTransaction:
                serializer = SafeModuleTransactionWithTransfersResponseSerializer
            elif model_type == MultisigTransaction:
                serializer = SafeMultisigTransactionWithTransfersResponseSerializer
            else:
                raise ValueError(f'Type={model_type} not expected, cannot serialize')
            serialized = serializer(model)
            # serialized.is_valid(raise_exception=True)
            results.append(serialized.data)
        return results
