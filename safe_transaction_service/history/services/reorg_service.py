import logging
from typing import Dict, Optional

from django.db import models, transaction

from hexbytes import HexBytes

from gnosis.eth import EthereumClient, EthereumClientProvider

from ..models import EthereumBlock, ProxyFactory, SafeContract, SafeMasterCopy

logger = logging.getLogger(__name__)


class ReorgServiceProvider:
    def __new__(cls):
        if not hasattr(cls, 'instance'):
            from django.conf import settings
            cls.instance = ReorgService(EthereumClientProvider(), settings.ETH_REORG_BLOCKS)
        return cls.instance

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, 'instance'):
            del cls.instance


# TODO Test ReorgService
class ReorgService:
    def __init__(self, ethereum_client: EthereumClient, eth_reorg_blocks: int,
                 eth_reorg_rewind_blocks: Optional[int] = 250):
        """
        :param ethereum_client:
        :param eth_reorg_blocks: Minimum number of blocks to consider a block confirmed and safe to rely on. By default
        250 blocks (1 hour)
        :param eth_reorg_back_blocks: Number of blocks to rewind when a reorg is found
        """
        self.ethereum_client = ethereum_client
        self.eth_reorg_blocks = eth_reorg_blocks  #
        self.eth_reorg_rewind_blocks = eth_reorg_rewind_blocks
        # Dictionary with Django model and attribute for reorgs
        self.reorg_models: Dict[models.Model, str] = {
            SafeMasterCopy: 'tx_block_number',
            ProxyFactory: 'tx_block_number',
            SafeContract: 'erc20_block_number',
        }

    def check_reorgs(self) -> Optional[int]:
        """
        :return: Number of oldest block with reorg detected. `None` if not reorg found
        """
        current_block_number = self.ethereum_client.current_block_number
        to_block = current_block_number - self.eth_reorg_blocks
        for database_block in EthereumBlock.objects.not_confirmed(to_block_number=to_block):
            blockchain_block = self.ethereum_client.get_block(database_block.number, full_transactions=False)
            if HexBytes(blockchain_block['hash']) == HexBytes(database_block.block_hash):
                database_block.set_confirmed()
            else:
                logger.warning('Reorg found for block-number=%d', database_block.number)
                return database_block.number

    def reset_all_to_block(self, block_number: int) -> int:
        """
        Reset database fields to a block to start reindexing from that block. It's useful when you want to trigger
        a indexation for txs that are not appearing on database but you don't want to delete anything
        :param block_number:
        :return: Number of updated models
        """
        updated = 0
        for model, field in self.reorg_models.items():
            updated += model.objects.filter(
                **{field + '__gt': block_number}
            ).update(
                **{field: block_number}
            )
        return updated

    @transaction.atomic
    def recover_from_reorg(self, first_reorg_block_number: int) -> int:
        """
        :param first_reorg_block_number:
        :return: Return number of elements updated
        """
        safe_reorg_block_number = max(first_reorg_block_number - self.eth_reorg_rewind_blocks, 0)

        updated = 0
        for model, field in self.reorg_models.items():
            updated += model.objects.filter(
                **{field + '__gte': first_reorg_block_number}
            ).update(
                **{field: safe_reorg_block_number}
            )

        EthereumBlock.objects.filter(number__gte=first_reorg_block_number).delete()
        logger.warning('Reorg of block-number=%d fixed, %d elements updated', first_reorg_block_number, updated)
        return updated
