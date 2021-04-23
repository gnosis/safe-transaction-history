import datetime
from enum import Enum
from itertools import islice
from logging import getLogger
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type, TypedDict

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import IntegrityError, models
from django.db.models import Case, Count, Index, JSONField, Q, QuerySet, Sum
from django.db.models.expressions import (F, OuterRef, RawSQL, Subquery, Value,
                                          When)
from django.db.models.signals import post_save

from hexbytes import HexBytes
from model_utils.models import TimeStampedModel

from gnosis.eth.constants import ERC20_721_TRANSFER_TOPIC
from gnosis.eth.django.models import (EthereumAddressField, HexField,
                                      Sha3HashField, Uint256Field)
from gnosis.safe import SafeOperation
from gnosis.safe.safe_signature import SafeSignature, SafeSignatureType

from safe_transaction_service.contracts.models import Contract

from .utils import clean_receipt_log

logger = getLogger(__name__)


class ConfirmationType(Enum):
    CONFIRMATION = 0
    EXECUTION = 1


class EthereumTxCallType(Enum):
    # https://ethereum.stackexchange.com/questions/63743/whats-the-difference-between-type-and-calltype-in-parity-trace
    CALL = 0
    DELEGATE_CALL = 1
    CALL_CODE = 2
    STATIC_CALL = 3

    @staticmethod
    def parse_call_type(call_type: Optional[str]):
        if not call_type:
            return None

        call_type = call_type.lower()
        if call_type == 'call':
            return EthereumTxCallType.CALL
        elif call_type == 'delegatecall':
            return EthereumTxCallType.DELEGATE_CALL
        elif call_type == 'callcode':
            return EthereumTxCallType.CALL_CODE
        elif call_type == 'staticcall':
            return EthereumTxCallType.STATIC_CALL
        else:
            return None


class InternalTxType(Enum):
    CALL = 0
    CREATE = 1
    SELF_DESTRUCT = 2
    REWARD = 3

    @staticmethod
    def parse(tx_type: str):
        tx_type = tx_type.upper()
        if tx_type == 'CALL':
            return InternalTxType.CALL
        elif tx_type == 'CREATE':
            return InternalTxType.CREATE
        elif tx_type in ('SUICIDE', 'SELFDESTRUCT'):
            return InternalTxType.SELF_DESTRUCT
        elif tx_type == 'REWARD':
            return InternalTxType.REWARD
        else:
            raise ValueError(f'{tx_type} is not a valid InternalTxType')


class TransferDict(TypedDict):
    block_number: int
    transaction_hash: HexBytes
    to: str
    _from: str
    value: int
    execution_date: datetime.datetime
    token_id: int
    token_address: str


class BulkCreateSignalMixin:
    def bulk_create(self, objs, batch_size: Optional[int] = None, ignore_conflicts: bool = False):
        objs = list(objs)  # If not it won't be iterate later
        result = super().bulk_create(objs, batch_size=batch_size, ignore_conflicts=ignore_conflicts)
        for obj in objs:
            post_save.send(obj.__class__, instance=obj, created=True)
        return result

    def bulk_create_from_generator(self, objs, batch_size: int = 10000, ignore_conflicts: bool = False) -> int:
        """
        Implementation in Django is not ok, as it will do `objs = list(objs)`. If objects come from a generator
        they will be brought to RAM. This approach is more friendly
        :return: Count of inserted elements
        """
        assert batch_size is not None and batch_size > 0
        total = 0
        while True:
            if inserted := len(self.bulk_create(islice(objs, batch_size), ignore_conflicts=ignore_conflicts)):
                total += inserted
            else:
                return total


class EthereumBlockManager(models.Manager):
    def get_or_create_from_block(self, block: Dict[str, Any], confirmed: bool = False):
        try:
            return self.get(number=block['number'])
        except self.model.DoesNotExist:
            return self.create_from_block(block, confirmed=confirmed)

    def create_from_block(self, block: Dict[str, Any], confirmed: bool = False) -> 'EthereumBlock':
        """
        :param block: Block Dict returned by Web3
        :param confirmed: If True we will not check for reorgs in the future
        :return: EthereumBlock model
        """
        try:
            return super().create(
                number=block['number'],
                gas_limit=block['gasLimit'],
                gas_used=block['gasUsed'],
                timestamp=datetime.datetime.fromtimestamp(block['timestamp'],
                                                          datetime.timezone.utc),
                block_hash=block['hash'],
                parent_hash=block['parentHash'],
                confirmed=confirmed,
            )
        except IntegrityError:
            # The block could be created in the meantime by other task while the block was fetched from blockchain
            return self.get(number=block['number'])


class EthereumBlockQuerySet(models.QuerySet):
    def not_confirmed(self, to_block_number: Optional[int] = None):
        queryset = self.filter(confirmed=False)
        if to_block_number is not None:
            queryset = queryset.filter(number__lte=to_block_number)
        return queryset.order_by('number')


class EthereumBlock(models.Model):
    objects = EthereumBlockManager.from_queryset(EthereumBlockQuerySet)()
    number = models.PositiveIntegerField(primary_key=True)
    gas_limit = models.PositiveIntegerField()
    gas_used = models.PositiveIntegerField()
    timestamp = models.DateTimeField()
    block_hash = Sha3HashField(unique=True)
    parent_hash = Sha3HashField(unique=True)
    # For reorgs, True if `current_block_number` - `number` >= MIN_CONFIRMATIONS
    confirmed = models.BooleanField(default=False, db_index=True)

    def __str__(self):
        return f'Block number={self.number} on {self.timestamp}'

    def set_confirmed(self):
        self.confirmed = True
        self.save(update_fields=['confirmed'])


class EthereumTxManager(models.Manager):
    def create_from_tx_dict(self, tx: Dict[str, Any], tx_receipt: Optional[Dict[str, Any]] = None,
                            ethereum_block: Optional[EthereumBlock] = None) -> 'EthereumTx':
        data = HexBytes(tx.get('data') or tx.get('input'))
        return super().create(
            block=ethereum_block,
            tx_hash=HexBytes(tx['hash']).hex(),
            _from=tx['from'],
            gas=tx['gas'],
            gas_price=tx['gasPrice'],
            gas_used=tx_receipt and tx_receipt['gasUsed'],
            logs=tx_receipt and [clean_receipt_log(log) for log in tx_receipt.get('logs', list())],
            status=tx_receipt and tx_receipt.get('status'),
            transaction_index=tx_receipt and tx_receipt['transactionIndex'],
            data=data if data else None,
            nonce=tx['nonce'],
            to=tx.get('to'),
            value=tx['value'],
        )


class EthereumTx(TimeStampedModel):
    objects = EthereumTxManager()
    block = models.ForeignKey(EthereumBlock, on_delete=models.CASCADE, null=True, default=None,
                              related_name='txs')  # If mined
    tx_hash = Sha3HashField(primary_key=True)
    gas_used = Uint256Field(null=True, default=None)  # If mined
    status = models.IntegerField(null=True, default=None, db_index=True)  # If mined. Old txs don't have `status`
    logs = ArrayField(JSONField(), null=True, default=None)  # If mined
    transaction_index = models.PositiveIntegerField(null=True, default=None)  # If mined
    _from = EthereumAddressField(null=True, db_index=True)
    gas = Uint256Field()
    gas_price = Uint256Field()
    data = models.BinaryField(null=True)
    nonce = Uint256Field()
    to = EthereumAddressField(null=True, db_index=True)
    value = Uint256Field()

    def __str__(self):
        return '{} status={} from={} to={}'.format(self.tx_hash, self.status, self._from, self.to)

    @property
    def execution_date(self) -> Optional[datetime.datetime]:
        if self.block_id is not None:
            return self.block.timestamp
        return None

    @property
    def success(self) -> Optional[bool]:
        if self.status is not None:
            return self.status == 1

    def update_with_block_and_receipt(self, ethereum_block: 'EthereumBlock', tx_receipt: Dict[str, Any]):
        if self.block is None:
            self.block = ethereum_block
            self.gas_used = tx_receipt['gasUsed']
            self.logs = [clean_receipt_log(log) for log in tx_receipt.get('logs', list())]
            self.status = tx_receipt.get('status')
            self.transaction_index = tx_receipt['transactionIndex']
            return self.save(update_fields=['block', 'gas_used', 'logs', 'status', 'transaction_index'])


class EthereumEventQuerySet(models.QuerySet):
    def not_erc_20_721_events(self):
        return self.exclude(topic=ERC20_721_TRANSFER_TOPIC)

    def erc20_and_721_events(self, token_address: Optional[str] = None, address: Optional[str] = None):
        queryset = self.filter(topic=ERC20_721_TRANSFER_TOPIC)
        if token_address:
            queryset = queryset.filter(address=token_address)
        if address:
            queryset = queryset.filter(Q(arguments__to=address) | Q(arguments__from=address))
        return queryset

    def erc20_events(self, token_address: Optional[str] = None, address: Optional[str] = None):
        return self.erc20_and_721_events(token_address=token_address,
                                         address=address).filter(arguments__has_key='value')

    def erc721_events(self, token_address: Optional[str] = None, address: Optional[str] = None):
        # TODO Sql with tokens registered as NFT
        return self.erc20_and_721_events(token_address=token_address,
                                         address=address).filter(arguments__has_key='tokenId')

    def erc721_owned_by(self, address: str) -> List[Tuple[str, int]]:
        """
        Returns erc721 owned by address, removing the ones sent
        :return: List of tuples(token_address: str, token_id: int)
        """
        # Get all the token history
        erc721_events = self.erc721_events(address=address)
        # Get tokens received and remove tokens transferred
        tokens_in: Tuple[str, int] = []
        tokens_out: Tuple[str, int] = []
        for erc721_event in erc721_events:
            token_address = erc721_event.address
            token_id = erc721_event.arguments.get('tokenId')
            if token_id is None:
                logger.error('TokenId for ERC721 info token=%s with owner=%s can never be None', token_address,
                             address)
                continue
            if erc721_event.arguments.get('to') == address:
                list_to_append = tokens_in
            else:
                list_to_append = tokens_out
            list_to_append.append((token_address, token_id))

        for token_out in tokens_out:  # Remove tokens sent from list
            if token_out in tokens_in:
                tokens_in.remove(token_out)
        return tokens_in


class EthereumEventManager(BulkCreateSignalMixin, models.Manager):
    def from_decoded_event(self, decoded_event: Dict[str, Any]) -> 'EthereumEvent':
        """
        Does not create the model. Requires that `ethereum_tx` exists
        :param decoded_event:
        :return: `EthereumEvent` instance (not stored in database)
        """
        return EthereumEvent(ethereum_tx_id=decoded_event['transactionHash'],
                             log_index=decoded_event['logIndex'],
                             address=decoded_event['address'],
                             topic=decoded_event['topics'][0],
                             topics=decoded_event['topics'],
                             arguments=decoded_event['args'])

    def erc20_tokens_used_by_address(self, address: str) -> List[str]:
        """
        :param address:
        :return: List of token addresses used by an address
        """
        # return self.erc20_events(address=address).values_list('address', flat=True).distinct()
        address_as_postgres_text = f'"{address}"'
        events = self.raw("""SELECT DISTINCT "id", "address" FROM "history_ethereumevent" WHERE
            ("topic" = %s
            AND (("arguments" -> 'to')::text = %s
            OR ("arguments" -> 'from')::text = %s)
            AND "arguments" ? 'value')
        """, [ERC20_721_TRANSFER_TOPIC[2:], address_as_postgres_text, address_as_postgres_text])
        return [event.address for event in events]

    def erc721_tokens_used_by_address(self, address: str) -> List[str]:
        """
        :param address:
        :return: List of token addresses used by an address
        """
        # return self.erc721_events(address=address).values_list('address', flat=True).distinct()
        address_as_postgres_text = f'"{address}"'
        events = self.raw("""SELECT DISTINCT "id", "address" FROM "history_ethereumevent" WHERE
                    ("topic" = '%s'
                    AND (("arguments" -> 'to')::text = '"%s"'
                    OR ("arguments" -> 'from')::text = '"%s"')
                    AND "arguments" ? 'tokenId')
        """, [ERC20_721_TRANSFER_TOPIC[2:], address_as_postgres_text, address_as_postgres_text])
        return [event.address for event in events]

    def erc20_tokens_with_balance(self, address: str) -> List[Dict[str, Any]]:
        """
        :return: List of dictionaries {'token_address': str, 'balance': int}
        """
        arguments_value_field = RawSQL("(arguments->>'value')::numeric", ())
        return self.erc20_events(
            address=address
        ).values('address').annotate(
            balance=Sum(Case(
                When(arguments__from=address, then=-arguments_value_field),
                default=arguments_value_field,
            ))
        ).order_by('-balance').values('address', 'balance')

    def get_or_create_erc20_or_721_event(self, decoded_event: Dict[str, Any]):
        if 'value' not in decoded_event['args'] or 'tokenId' not in decoded_event['args']:
            raise ValueError('Invalid ERC20 or ERC721 event %s' % decoded_event)
        else:
            return self.get_or_create(ethereum_tx_id=decoded_event['transactionHash'],
                                      log_index=decoded_event['logIndex'],
                                      defaults={'address': decoded_event['address'],
                                                'topic': decoded_event['topics'][0],
                                                'topics': decoded_event['topics'],
                                                'arguments': decoded_event['args'],
                                                })


class EthereumEvent(models.Model):
    objects = EthereumEventManager.from_queryset(EthereumEventQuerySet)()
    ethereum_tx = models.ForeignKey(EthereumTx, on_delete=models.CASCADE, related_name='events')
    log_index = models.PositiveIntegerField()
    address = EthereumAddressField(db_index=True)
    topic = Sha3HashField(db_index=True)
    topics = ArrayField(Sha3HashField())
    arguments = JSONField()

    class Meta:
        indexes = [GinIndex(fields=['arguments'])]
        unique_together = (('ethereum_tx', 'log_index'),)
        # There are also 2 indexes created manually by 0026 migration, both Btree for arguments->to and arguments->from
        # To use that indexes json queries must be rewritten to use `::text` fields

    def __str__(self):
        return f'Tx-hash={self.ethereum_tx_id} Log-index={self.log_index} Topic={self.topic} Arguments={self.arguments}'

    @property
    def created(self):
        return self.ethereum_tx.block.timestamp

    def is_erc20(self) -> bool:
        return self.topic == ERC20_721_TRANSFER_TOPIC and 'value' in self.arguments and 'to' in self.arguments

    def is_erc721(self) -> bool:
        return self.topic == ERC20_721_TRANSFER_TOPIC and 'tokenId' in self.arguments and 'to' in self.arguments


class InternalTxManager(BulkCreateSignalMixin, models.Manager):
    def _trace_address_to_str(self, trace_address: Sequence[int]) -> str:
        return ','.join([str(address) for address in trace_address])

    def build_from_trace(self, trace: Dict[str, Any], ethereum_tx: EthereumTx) -> 'InternalTx':
        """
        Build a InternalTx object from trace, but it doesn't insert it on database
        :param trace:
        :param ethereum_tx:
        :return: InternalTx not inserted
        """
        data = trace['action'].get('input') or trace['action'].get('init')
        tx_type = InternalTxType.parse(trace['type'])
        call_type = EthereumTxCallType.parse_call_type(trace['action'].get('callType'))
        trace_address_str = self._trace_address_to_str(trace['traceAddress'])
        return InternalTx(
            ethereum_tx=ethereum_tx,
            trace_address=trace_address_str,
            _from=trace['action'].get('from'),
            gas=trace['action'].get('gas', 0),
            data=data if data else None,
            to=trace['action'].get('to') or trace['action'].get('address'),
            value=trace['action'].get('value') or trace['action'].get('balance', 0),
            gas_used=(trace.get('result') or {}).get('gasUsed', 0),
            contract_address=(trace.get('result') or {}).get('address'),
            code=(trace.get('result') or {}).get('code'),
            output=(trace.get('result') or {}).get('output'),
            refund_address=trace['action'].get('refundAddress'),
            tx_type=tx_type.value,
            call_type=call_type.value if call_type else None,
            error=trace.get('error')
        )

    def get_or_create_from_trace(self, trace: Dict[str, Any], ethereum_tx: EthereumTx) -> Tuple['InternalTx', bool]:
        tx_type = InternalTxType.parse(trace['type'])
        call_type = EthereumTxCallType.parse_call_type(trace['action'].get('callType'))
        trace_address_str = self._trace_address_to_str(trace['traceAddress'])
        return self.get_or_create(
            ethereum_tx=ethereum_tx,
            trace_address=trace_address_str,
            defaults={
                '_from': trace['action'].get('from'),
                'gas': trace['action'].get('gas', 0),
                'data': trace['action'].get('input') or trace['action'].get('init'),
                'to': trace['action'].get('to') or trace['action'].get('address'),
                'value': trace['action'].get('value') or trace['action'].get('balance', 0),
                'gas_used': (trace.get('result') or {}).get('gasUsed', 0),
                'contract_address': (trace.get('result') or {}).get('address'),
                'code': (trace.get('result') or {}).get('code'),
                'output': (trace.get('result') or {}).get('output'),
                'refund_address': trace['action'].get('refundAddress'),
                'tx_type': tx_type.value,
                'call_type': call_type.value if call_type else None,
                'error': trace.get('error'),
            }
        )


class InternalTxQuerySet(models.QuerySet):
    def ether_txs(self):
        return self.filter(
            call_type=EthereumTxCallType.CALL.value,
            value__gt=0
        ).annotate(
            transaction_hash=F('ethereum_tx_id'),
            block_number=F('ethereum_tx__block_id'),
            execution_date=F('ethereum_tx__block__timestamp'),
            token_id=Value(None, output_field=Uint256Field()),
            token_address=Value(None, output_field=EthereumAddressField()),
        ).order_by('-ethereum_tx__block_id')

    def ether_txs_for_address(self, address: str):
        return self.ether_txs().filter(Q(to=address) | Q(_from=address))

    def ether_incoming_txs_for_address(self, address: str):
        return self.ether_txs().filter(to=address)

    def token_txs(self):
        return EthereumEvent.objects.erc20_and_721_events().annotate(
            to=RawSQL("arguments->>%s", ('to',)),  # Order is really important!
            _from=RawSQL("arguments->>%s", ('from',)),
            value=RawSQL("(arguments->>%s)::numeric", ('value',)),
            transaction_hash=F('ethereum_tx_id'),
            block_number=F('ethereum_tx__block_id'),
            execution_date=F('ethereum_tx__block__timestamp'),
            token_id=RawSQL("(arguments->>%s)::numeric", ('tokenId',)),
            token_address=F('address')
        ).order_by('-ethereum_tx__block_id')

    def token_txs_for_address(self, address: str):
        return self.token_txs().filter(Q(arguments__to=address) | Q(arguments__from=address))

    def token_incoming_txs_for_address(self, address: str):
        return self.token_txs().filter(arguments__to=address)

    def ether_and_token_txs(self, address: str):
        tokens_queryset = self.token_txs_for_address(address)
        ether_queryset = self.ether_txs_for_address(address)
        return self.union_ether_and_token_txs(tokens_queryset, ether_queryset)

    def ether_and_token_incoming_txs(self, address: str):
        tokens_queryset = self.token_incoming_txs_for_address(address)
        ether_queryset = self.ether_incoming_txs_for_address(address)
        return self.union_ether_and_token_txs(tokens_queryset, ether_queryset)

    def union_ether_and_token_txs(self, tokens_queryset: QuerySet, ether_queryset: QuerySet) -> TransferDict:
        values = ('block_number', 'transaction_hash', 'to', '_from', 'value', 'execution_date', 'token_id',
                  'token_address')
        return ether_queryset.values(*values).union(tokens_queryset.values(*values)).order_by('-block_number')

    def can_be_decoded(self):
        """
        Every InternalTx can be decoded if:
            - Has data
            - InternalTx is not errored
            - EthereumTx is successful (not reverted or out of gas)
            - CallType is a DELEGATE_CALL (to the master copy contract)
            - Not already decoded
        :return: Txs that can be decoded
        """
        return self.exclude(
            data=None
        ).filter(
            call_type=EthereumTxCallType.DELEGATE_CALL.value,
            error=None,
            ethereum_tx__status=1,
            decoded_tx=None,
        )


class InternalTx(models.Model):
    objects = InternalTxManager.from_queryset(InternalTxQuerySet)()
    ethereum_tx = models.ForeignKey(EthereumTx, on_delete=models.CASCADE, related_name='internal_txs')
    _from = EthereumAddressField(null=True, db_index=True)  # For SELF-DESTRUCT it can be null
    gas = Uint256Field()
    data = models.BinaryField(null=True)  # `input` for Call, `init` for Create
    to = EthereumAddressField(null=True, db_index=True)
    value = Uint256Field()
    gas_used = Uint256Field()
    contract_address = EthereumAddressField(null=True, db_index=True)  # Create
    code = models.BinaryField(null=True)                # Create
    output = models.BinaryField(null=True)              # Call
    refund_address = EthereumAddressField(null=True, db_index=True)  # For SELF-DESTRUCT
    tx_type = models.PositiveSmallIntegerField(choices=[(tag.value, tag.name) for tag in InternalTxType], db_index=True)
    call_type = models.PositiveSmallIntegerField(null=True,
                                                 choices=[(tag.value, tag.name) for tag in EthereumTxCallType],
                                                 db_index=True)  # Call
    trace_address = models.CharField(max_length=600)  # Stringified traceAddress
    error = models.CharField(max_length=200, null=True)

    class Meta:
        unique_together = (('ethereum_tx', 'trace_address'),)

    def __str__(self):
        if self.to:
            return 'Internal tx hash={} from={} to={}'.format(self.ethereum_tx_id, self._from, self.to)
        else:
            return 'Internal tx hash={} from={}'.format(self.ethereum_tx_id, self._from)

    @property
    def block_number(self):
        return self.ethereum_tx.block_id

    @property
    def created(self):
        return self.ethereum_tx.block.timestamp

    @property
    def can_be_decoded(self) -> bool:
        return bool(self.is_delegate_call
                    and not self.error
                    and self.data
                    and self.ethereum_tx.success)

    @property
    def is_call(self):
        return InternalTxType(self.tx_type) == InternalTxType.CALL

    @property
    def is_create(self):
        return InternalTxType(self.tx_type) == InternalTxType.CREATE

    @property
    def is_decoded(self):
        try:
            return bool(self.decoded_tx)
        except InternalTxDecoded.DoesNotExist:
            return False

    @property
    def is_delegate_call(self) -> bool:
        if self.call_type is None:
            return False
        else:
            return EthereumTxCallType(self.call_type) == EthereumTxCallType.DELEGATE_CALL

    @property
    def is_ether_transfer(self) -> bool:
        return self.call_type == EthereumTxCallType.CALL.value and self.value > 0

    @property
    def is_relevant(self):
        return self.can_be_decoded or self.is_ether_transfer or self.contract_address

    @property
    def trace_address_as_list(self) -> List[int]:
        if not self.trace_address:
            return []
        else:
            return [int(x) for x in self.trace_address.split(',')]


class InternalTxDecodedManager(BulkCreateSignalMixin, models.Manager):
    pass


class InternalTxDecodedQuerySet(models.QuerySet):
    def for_safe(self, safe_address: str):
        """
        :param safe_address:
        :return: Queryset of all InternalTxDecoded for one Safe with `safe_address`
        """
        return self.filter(
            internal_tx___from=safe_address
        )

    def for_indexed_safes(self):
        """
        :return: Queryset of InternalTxDecoded for Safes already indexed or calling `setup`. Use this to index Safes
        for the first time
        """
        return self.filter(
            Q(internal_tx___from__in=SafeContract.objects.values('address'))  # Just Safes indexed
            | Q(function_name='setup')  # This way we can index new Safes without events
        )

    def not_processed(self):
        return self.filter(processed=False)

    def order_by_processing_queue(self):
        """
        :return: Transactions ordered to be processed. First older transactions
        """
        return self.order_by(
            'internal_tx__ethereum_tx__block_id',
            'internal_tx__ethereum_tx__transaction_index',
            'internal_tx__trace_address',
        )

    def pending_for_safes(self):
        """
        :return: Pending `InternalTxDecoded` sorted by block number and then transaction index inside the block
        """
        return self.not_processed(
        ).for_indexed_safes(
        ).select_related(
            'internal_tx__ethereum_tx__block',
        ).order_by_processing_queue()

    def pending_for_safe(self, safe_address: str):
        """
        :return: Pending `InternalTxDecoded` sorted by block number and then transaction index inside the block
        """
        return self.pending_for_safes().filter(internal_tx___from=safe_address)

    def safes_pending_to_be_processed(self) -> QuerySet:
        """
        :return: List of Safe addresses that have transactions pending to be processed
        """
        return self.not_processed().for_indexed_safes().values_list(
            'internal_tx___from', flat=True
        ).distinct('internal_tx___from')


class InternalTxDecoded(models.Model):
    objects = InternalTxDecodedManager.from_queryset(InternalTxDecodedQuerySet)()
    internal_tx = models.OneToOneField(InternalTx, on_delete=models.CASCADE, related_name='decoded_tx',
                                       primary_key=True)
    function_name = models.CharField(max_length=256, db_index=True)
    arguments = JSONField()
    processed = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(name='history_decoded_processed_idx',
                         fields=['processed'],
                         condition=Q(processed=False))
        ]
        verbose_name_plural = 'Internal txs decoded'

    def __str__(self):
        return f'{"Processed" if self.processed else "Not Processed"} ' \
               f'fn-name={self.function_name} with arguments={self.arguments}'

    @property
    def address(self) -> str:
        return self.internal_tx._from

    @property
    def block_number(self) -> Type[int]:
        return self.internal_tx.ethereum_tx.block_id

    @property
    def tx_hash(self) -> Type[int]:
        return self.internal_tx.ethereum_tx_id

    def set_processed(self):
        self.processed = True
        return self.save(update_fields=['processed'])


class MultisigTransactionManager(models.Manager):
    def last_nonce(self, safe: str) -> Optional[int]:
        """
        :param safe:
        :return: nonce of the last executed and mined transaction. It will be None if there's no transactions or none
        of them is mined
        """
        nonce_query = self.filter(safe=safe).exclude(ethereum_tx=None).order_by('-nonce').values('nonce').first()
        if nonce_query:
            return nonce_query['nonce']

    def last_valid_transaction(self, safe: str) -> Optional['MultisigTransaction']:
        """
        Find last transaction where signers match the owners registered for that Safe. Transactions out of sync
        have an invalid `safeNonce`, so `safeTxHash` is not valid and owners recovered from the signatures wouldn't be
        valid. We exclude `Approved hashes` and `Contract signatures` as that owners are not retrieved using the
        signature, so they will show the right owner even if `safeNonce` is not valid
        :param safe:
        :return: Last valid indexed transaction mined
        """
        # Build list of every owner known for that Safe (even if it was deleted/replaced). Changes of collision for
        # invalid recovered owners from signatures are almost impossible
        owners_set = set()
        for owners_list in SafeStatus.objects.filter(address=safe).values_list('owners', flat=True).distinct():
            owners_set.update(owners_list)

        return MultisigTransaction.objects.filter(
            safe=safe,
            confirmations__owner__in=owners_set,
            confirmations__signature_type__in=[SafeSignatureType.EOA.value,
                                               SafeSignatureType.ETH_SIGN.value]
        ).exclude(
            ethereum_tx=None
        ).order_by('-nonce').first()

    def safes_with_number_of_transactions_executed(self):
        return self.executed().values('safe').annotate(transactions=Count('safe')).order_by('-transactions')

    def safes_with_number_of_transactions_executed_and_master_copy(self):
        master_copy_query = SafeStatus.objects.filter(
            address=OuterRef('safe')
        ).order_by('-nonce').values('master_copy')

        return self.safes_with_number_of_transactions_executed(
        ).annotate(
            master_copy=Subquery(master_copy_query[:1])
        ).order_by('-transactions')

    def not_indexed_metadata_contract_addresses(self):
        """
        Find contracts with metadata not indexed
        :return:
        """
        return MultisigTransaction.objects.exclude(
            data=None
        ).exclude(
            to__in=Contract.objects.values('address')
        ).values_list('to', flat=True).distinct()


class MultisigTransactionQuerySet(models.QuerySet):
    def executed(self):
        return self.exclude(
            ethereum_tx__block=None
        )

    def not_executed(self):
        return self.filter(
            ethereum_tx__block=None
        )

    def with_confirmations(self):
        return self.exclude(
            confirmations__isnull=True
        )

    def without_confirmations(self):
        return self.filter(
            confirmations__isnull=True
        )

    def with_confirmations_required(self):
        """
        Add confirmations required for execution when the tx was mined (threshold of the Safe at that point)
        :return: queryset with `confirmations_required: int` field
        """
        threshold_query = SafeStatus.objects.filter(
            internal_tx__ethereum_tx=OuterRef('ethereum_tx')
        ).sorted_reverse_by_internal_tx().values('threshold')

        return self.annotate(confirmations_required=Subquery(threshold_query[:1]))


class MultisigTransaction(TimeStampedModel):
    objects = MultisigTransactionManager.from_queryset(MultisigTransactionQuerySet)()
    safe_tx_hash = Sha3HashField(primary_key=True)
    safe = EthereumAddressField(db_index=True)
    ethereum_tx = models.ForeignKey(EthereumTx, null=True, default=None, blank=True,
                                    on_delete=models.SET_NULL, related_name='multisig_txs')
    to = EthereumAddressField(null=True, db_index=True)
    value = Uint256Field()
    data = models.BinaryField(null=True)
    operation = models.PositiveSmallIntegerField(choices=[(tag.value, tag.name) for tag in SafeOperation])
    safe_tx_gas = Uint256Field()
    base_gas = Uint256Field()
    gas_price = Uint256Field()
    gas_token = EthereumAddressField(null=True)
    refund_receiver = EthereumAddressField(null=True)
    signatures = models.BinaryField(null=True)  # When tx is executed
    nonce = Uint256Field(db_index=True)
    failed = models.BooleanField(null=True, default=None, db_index=True)
    origin = models.CharField(null=True, default=None, max_length=200)  # To store arbitrary data on the tx
    trusted = models.BooleanField(default=False, db_index=True)  # Txs proposed by a delegate or with one confirmation

    def __str__(self):
        return f'{self.safe} - {self.nonce} - {self.safe_tx_hash}'

    @property
    def execution_date(self) -> Optional[datetime.datetime]:
        if self.ethereum_tx_id and self.ethereum_tx.block_id is not None:
            return self.ethereum_tx.block.timestamp
        return None

    @property
    def executed(self) -> bool:
        return bool(self.ethereum_tx_id and (self.ethereum_tx.block_id is not None))

    @property
    def owners(self) -> Optional[List[str]]:
        if not self.signatures:
            return None
        else:
            signatures = bytes(self.signatures)
            safe_signatures = SafeSignature.parse_signature(signatures, self.safe_tx_hash)
            return [safe_signature.owner for safe_signature in safe_signatures]


class ModuleTransaction(TimeStampedModel):
    internal_tx = models.OneToOneField(InternalTx, on_delete=models.CASCADE, related_name='module_tx',
                                       primary_key=True)
    safe = EthereumAddressField(db_index=True)  # Just for convenience, it could be retrieved from `internal_tx`
    module = EthereumAddressField(db_index=True)  # Just for convenience, it could be retrieved from `internal_tx`
    to = EthereumAddressField(db_index=True)
    value = Uint256Field()
    data = models.BinaryField(null=True)
    operation = models.PositiveSmallIntegerField(choices=[(tag.value, tag.name) for tag in SafeOperation])
    failed = models.BooleanField(default=False)

    def __str__(self):
        if self.value:
            return f'{self.safe} - {self.to} - {self.value}'
        else:
            return f'{self.safe} - {self.to} - {HexBytes(self.data.tobytes()).hex()[:8]}'

    @property
    def execution_date(self) -> Optional[datetime.datetime]:
        if self.internal_tx.ethereum_tx_id and self.internal_tx.ethereum_tx.block_id is not None:
            return self.internal_tx.ethereum_tx.block.timestamp
        return None


class MultisigConfirmationManager(models.Manager):
    def remove_unused_confirmations(self, safe: str, current_safe_none: int, owner: str) -> int:
        """
        :return: Remove confirmations for not executed transactions with nonce higher or equal than
        the current Safe nonce for a Safe and an owner (as an owner can be an owner of multiple Safes).
        Used when an owner is removed from the Safe.
        """
        return self.filter(
            multisig_transaction__ethereum_tx=None,  # Not executed
            multisig_transaction__safe=safe,
            multisig_transaction__nonce__gte=current_safe_none,
            owner=owner,
        ).delete()[0]


class MultisigConfirmationQuerySet(models.QuerySet):
    def without_transaction(self):
        return self.filter(multisig_transaction=None)

    def with_transaction(self):
        return self.exclude(multisig_transaction=None)


class MultisigConfirmation(TimeStampedModel):
    objects = MultisigConfirmationManager.from_queryset(MultisigConfirmationQuerySet)()
    ethereum_tx = models.ForeignKey(EthereumTx, on_delete=models.CASCADE, related_name='multisig_confirmations',
                                    null=True)  # `null=True` for signature confirmations
    multisig_transaction = models.ForeignKey(MultisigTransaction,
                                             on_delete=models.CASCADE,
                                             null=True,
                                             related_name='confirmations')
    multisig_transaction_hash = Sha3HashField(null=True,
                                              db_index=True)  # Use this while we don't have a `multisig_transaction`
    owner = EthereumAddressField()

    signature = HexField(null=True, default=None, max_length=2000)
    signature_type = models.PositiveSmallIntegerField(choices=[(tag.value, tag.name) for tag in SafeSignatureType],
                                                      db_index=True)

    class Meta:
        unique_together = (('multisig_transaction_hash', 'owner'),)
        ordering = ['created']

    def __str__(self):
        if self.multisig_transaction_id:
            return f'Confirmation of owner={self.owner} for transaction-hash={self.multisig_transaction_hash}'
        else:
            return f'Confirmation of owner={self.owner} for existing transaction={self.multisig_transaction_hash}'


class MonitoredAddressManager(models.Manager):
    def update_addresses(self, addresses: List[str], from_block_number: int, block_number: int,
                         database_field: str) -> int:
        """
        :param addresses: Addresses to have the block number updated
        :param from_block_number: Make sure that no reorg has happened checking that block number was not rollbacked
        :param block_number: Block number to be updated
        :param database_field: Database field to store the block number
        :return: Number of entities updated
        """
        return self.filter(
            **{'address__in': addresses,
               database_field + '__gte': from_block_number - 1,  # Protect in case of reorg
               }
        ).update(**{database_field: block_number})


class MonitoredAddressQuerySet(models.QuerySet):
    def almost_updated(self, database_field: str, current_block_number: int,
                       updated_blocks_behind: int, confirmations: int):
        return self.filter(
            **{database_field + '__lt': current_block_number - confirmations,
               database_field + '__gt': current_block_number - updated_blocks_behind})

    def not_updated(self, database_field: str, current_block_number: int, confirmations: int):
        return self.filter(
            **{database_field + '__lt': current_block_number - confirmations}
        )


class MonitoredAddress(models.Model):
    objects = MonitoredAddressManager.from_queryset(MonitoredAddressQuerySet)()
    address = EthereumAddressField(primary_key=True)
    initial_block_number = models.IntegerField(default=0)  # Block number when address received first tx
    tx_block_number = models.IntegerField(null=True, default=None,
                                          db_index=True)  # Block number when last internal tx scan ended

    class Meta:
        abstract = True
        verbose_name_plural = 'Monitored addresses'

    def __str__(self):
        return f'Address={self.address} - Initial-block-number={self.initial_block_number}' \
               f' - Tx-block-number={self.tx_block_number}'


class ProxyFactory(MonitoredAddress):
    class Meta:
        verbose_name_plural = 'Proxy factories'
        ordering = ['tx_block_number']


class SafeL2MasterCopy(MonitoredAddress):
    class Meta:
        verbose_name_plural = 'Safe L2 master copies'
        ordering = ['tx_block_number']


class SafeMasterCopy(MonitoredAddress):
    version = models.CharField(max_length=20)
    deployer = models.CharField(max_length=50, default='Gnosis')

    class Meta:
        verbose_name_plural = 'Safe master copies'
        ordering = ['tx_block_number']


class SafeContractManager(MonitoredAddressManager):
    pass


class SafeContract(models.Model):
    objects = SafeContractManager.from_queryset(MonitoredAddressQuerySet)()
    address = EthereumAddressField(primary_key=True)
    ethereum_tx = models.ForeignKey(EthereumTx, on_delete=models.CASCADE, related_name='safe_contracts')
    erc20_block_number = models.IntegerField(default=0, db_index=True)  # Block number of last scan of erc20

    def __str__(self):
        return f'Safe address={self.address} - ethereum-tx={self.ethereum_tx_id}'

    @property
    def created(self):
        return self.ethereum_tx.block.timestamp

    @property
    def created_block_number(self) -> Optional[Type[int]]:
        if self.ethereum_tx:
            return self.ethereum_tx.block_id


class SafeContractDelegateManager(models.Manager):
    def get_delegates_for_safe(self, address: str) -> List[str]:
        return list(self.filter(safe_contract_id=address).values_list('delegate', flat=True))


class SafeContractDelegate(models.Model):
    """
    The owners of the Safe can add users so they can propose/retrieve txs as if they were the owners of the Safe
    """
    objects = SafeContractDelegateManager()
    safe_contract = models.ForeignKey(SafeContract, on_delete=models.CASCADE, related_name='safe_contract_delegates')
    delegate = EthereumAddressField()
    delegator = EthereumAddressField()  # Owner who created the delegate
    label = models.CharField(max_length=50)
    read = models.BooleanField(default=True)  # For permissions in the future
    write = models.BooleanField(default=True)

    class Meta:
        unique_together = (('safe_contract', 'delegate'),)

    def __str__(self):
        return f'Delegate={self.delegate} for Safe={self.safe_contract_id} - Label={self.label}'


class SafeStatusManager(models.Manager):
    pass


class SafeStatusQuerySet(models.QuerySet):
    def sorted_by_internal_tx(self):
        """
        Last SafeStatus first. Usually ordering by `nonce` it should be enough, but in some cases (MultiSend)
        there could be multiple transactions with the same nonce. `address` must be part of the expression to use
        `distinct()` later
        :return: SafeStatus QuerySet sorted
        """
        return self.order_by(
            'address',
            '-nonce',
            '-internal_tx__ethereum_tx__block_id',
            '-internal_tx__ethereum_tx__transaction_index',
            '-internal_tx__trace_address',
        )

    def sorted_reverse_by_internal_tx(self):
        return self.order_by(
            'address',
            'nonce',
            'internal_tx__ethereum_tx__block_id',
            'internal_tx__ethereum_tx__transaction_index',
            'internal_tx__trace_address',
        )

    def addresses_for_owner(self, owner_address: str) -> List[str]:
        return self.filter(
            owners__contains=[owner_address],
            internal_tx__in=self.last_for_every_address().values('pk')
        ).values_list('address', flat=True)

    def last_for_every_address(self) -> QuerySet:
        return self.distinct(
            'address'  # Uses PostgreSQL `DISTINCT ON`
        ).select_related(
            'internal_tx__ethereum_tx'
        ).sorted_by_internal_tx()

    def last_for_address(self, address: str) -> Optional['SafeStatus']:
        return self.filter(
            address=address
        ).sorted_by_internal_tx().first()


class SafeStatus(models.Model):
    objects = SafeStatusManager.from_queryset(SafeStatusQuerySet)()
    internal_tx = models.OneToOneField(InternalTx, on_delete=models.CASCADE, related_name='safe_status',
                                       primary_key=True)
    address = EthereumAddressField(db_index=True)
    owners = ArrayField(EthereumAddressField())
    threshold = Uint256Field()
    nonce = Uint256Field(default=0)
    master_copy = EthereumAddressField()
    fallback_handler = EthereumAddressField()
    guard = EthereumAddressField(default=None, null=True)
    enabled_modules = ArrayField(EthereumAddressField(), default=list)

    class Meta:
        indexes = [
            Index(fields=['address', '-nonce']),   # Index on address and nonce DESC
            GinIndex(fields=['owners'])
        ]
        unique_together = (('internal_tx', 'address'),)
        verbose_name_plural = 'Safe statuses'

    def __str__(self):
        return f'safe={self.address} threshold={self.threshold} owners={self.owners} nonce={self.nonce}'

    @property
    def block_number(self):
        return self.internal_tx.ethereum_tx.block_id

    def is_corrupted(self):
        """
        SafeStatus nonce must be incremental. If current nonce is bigger than the number of SafeStatus for that Safe
        something is wrong. There could be more SafeStatus than nonce (e.g. a call to a MultiSend
        adding owners and enabling a Module in the same contract `execTransaction`)
        :return: True if corrupted, False otherwise
        """
        return self.__class__.objects.filter(
            address=self.address,
            nonce__lte=self.nonce
        ).count() <= self.nonce

    def store_new(self, internal_tx: InternalTx) -> None:
        self.internal_tx = internal_tx
        return self.save()


class WebHookType(Enum):
    NEW_CONFIRMATION = 0
    PENDING_MULTISIG_TRANSACTION = 1
    EXECUTED_MULTISIG_TRANSACTION = 2
    INCOMING_ETHER = 3
    INCOMING_TOKEN = 4
    CONFIRMATION_REQUEST = 5
    SAFE_CREATED = 6
    MODULE_TRANSACTION = 7
    OUTGOING_ETHER = 8
    OUTGOING_TOKEN = 9


class WebHookQuerySet(models.QuerySet):
    def matching_for_address(self, address: str):
        return self.filter(Q(address=address) | Q(address=''))


class WebHook(models.Model):
    objects = WebHookQuerySet.as_manager()
    address = EthereumAddressField(db_index=True, blank=True)
    url = models.URLField()
    # Configurable webhook types to listen to
    new_confirmation = models.BooleanField(default=True)
    pending_outgoing_transaction = models.BooleanField(default=True)
    new_executed_outgoing_transaction = models.BooleanField(default=True)
    new_incoming_transaction = models.BooleanField(default=True)
    new_safe = models.BooleanField(default=True)
    new_module_transaction = models.BooleanField(default=True)
    new_outgoing_transaction = models.BooleanField(default=True)

    class Meta:
        unique_together = (('address', 'url'),)

    def __str__(self):
        if self.address:
            return f'Webhook for safe={self.address} to url={self.url}'
        else:
            return f'Webhook to every address to url={self.url}'

    def is_valid_for_webhook_type(self, webhook_type: WebHookType):
        if webhook_type == WebHookType.NEW_CONFIRMATION and not self.new_confirmation:
            return False
        elif webhook_type == WebHookType.PENDING_MULTISIG_TRANSACTION and not self.pending_outgoing_transaction:
            return False
        elif webhook_type == WebHookType.EXECUTED_MULTISIG_TRANSACTION and not self.new_executed_outgoing_transaction:
            return False
        elif webhook_type in (WebHookType.INCOMING_TOKEN,
                              WebHookType.INCOMING_ETHER) and not self.new_incoming_transaction:
            return False
        elif webhook_type == WebHookType.SAFE_CREATED and not self.new_safe:
            return False
        elif webhook_type == WebHookType.MODULE_TRANSACTION and not self.new_module_transaction:
            return False
        elif webhook_type in (WebHookType.OUTGOING_TOKEN,
                              WebHookType.OUTGOING_ETHER) and not self.new_outgoing_transaction:
            return False
        else:
            return True
