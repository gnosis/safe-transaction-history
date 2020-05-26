import logging
import operator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import requests
from cachetools import cachedmethod
from web3 import Web3

from gnosis.eth import EthereumClient, EthereumClientProvider
from gnosis.eth.contracts import get_erc721_contract

from ..models import EthereumEvent

logger = logging.getLogger(__name__)


class CollectiblesServiceException(Exception):
    pass


class MetadataRetrievalException(CollectiblesServiceException):
    pass


@dataclass
class Collectible:
    token_name: str
    token_symbol: str
    address: str
    id: str
    uri: str


@dataclass
class CollectibleWithMetadata(Collectible):
    metadata: Dict[str, Any]
    name: Optional[str] = field(init=False)
    description: Optional[str] = field(init=False)
    image_uri: Optional[str] = field(init=False)

    def get_name(self) -> Optional[str]:
        if self.metadata:
            for key in ('name',):
                if key in self.metadata:
                    return self.metadata[key]

    def get_description(self) -> Optional[str]:
        if self.metadata:
            for key in ('description',):
                if key in self.metadata:
                    return self.metadata[key]

    def get_metadata_image(self) -> Optional[str]:
        if not self.metadata:
            return None

        for key in ('image', 'image_url', 'image_uri', 'imageUri', 'imageUrl'):
            if key in self.metadata:
                return self.metadata[key]

        for key, value in self.metadata.items():
            if (key.lower().startswith('image')
                    and isinstance(self.metadata[key], str)
                    and self.metadata[key].startswith('http')):
                return self.metadata[key]

    def __post_init__(self):
        self.name = self.get_name()
        self.description = self.get_description()
        self.image_uri = self.get_metadata_image()


class CollectiblesServiceProvider:
    def __new__(cls):
        if not hasattr(cls, 'instance'):
            from django.conf import settings
            cls.instance = CollectiblesService(EthereumClientProvider())

        return cls.instance

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, "instance"):
            del cls.instance


class CollectiblesService:
    def __init__(self, ethereum_client: EthereumClient):
        self.ethereum_client = ethereum_client
        self.cache_token_info = {}
        self.crypto_kitties_contract_addresses = {
            '0x06012c8cf97BEaD5deAe237070F9587f8E7A266d',  # Mainnet
            '0x16baF0dE678E52367adC69fD067E5eDd1D33e3bF'  # Rinkeby

        }
        self.ens_contract_address = {
            '0x57f1887a8BF19b14fC0dF6Fd9B2acc9Af147eA85',  # ENS Mainnet and Rinkeby
        }

    def get_collectibles_from_erc71_addresses(self, safe_address: str) -> List[Collectible]:
        """
        Gets collectibles without knowing the tokenIds, tries to retrieve them
        :param safe_address:
        :return:
        """
        assert Web3.isChecksumAddress(safe_address), f'Not valid address {safe_address} for getting collectibles'

        erc721_addresses = list(EthereumEvent.objects.erc721_tokens_used_by_address(safe_address))
        if not erc721_addresses:
            return []

        collectibles = []

        # TODO: Manage errors for one collectible
        # erc721_contracts = [get_erc721_contract(self.ethereum_client.w3, erc721_address)
        #                     for erc721_address in erc721_addresses]
        # balances = self.ethereum_client.batch_call(
        #            [erc_721_contract.functions.balanceOf(safe_address) for erc_721_contract in erc721_contracts]
        #        )
        for erc721_address in erc721_addresses:
            erc_721_contract = get_erc721_contract(self.ethereum_client.w3, erc721_address)
            token_info = self.get_token_info(erc721_address)
            if not token_info:
                name, symbol = ('', '')
            else:
                name, symbol = token_info
                if (len(name) - len(symbol)) < -5:  # If symbol is way bigger than name, swap them (e.g. POAP)
                    name, symbol = symbol, name
            try:
                balance = erc_721_contract.functions.balanceOf(safe_address).call()
                token_ids = self.ethereum_client.batch_call(
                    [erc_721_contract.functions.tokenOfOwnerByIndex(safe_address, i) for i in range(balance)]
                )
                token_uris = self.ethereum_client.batch_call(
                    [erc_721_contract.functions.tokenURI(token_id) for token_id in token_ids]
                )
                for token_id, token_uri in zip(token_ids, token_uris):
                    collectibles.append(Collectible(name, symbol, erc721_address, token_id, token_uri))
            except ValueError:
                logger.warning('Cannot get ERC721 info token=%s with owner=%s',
                               erc721_address, safe_address, exc_info=True)
        return collectibles

    def get_collectibles(self, safe_address: str) -> List[Collectible]:
        # Get all the token history
        erc721_events = EthereumEvent.objects.erc721_events(address=safe_address)
        # Check ownership of the tokens
        collectibles = []

        ownership_queries = []
        token_uri_queries = []
        for erc721_event in erc721_events:
            token_id = erc721_event.arguments.get('tokenId')
            if token_id is None:
                logger.error('TokenId for ERC721 info token=%s with owner=%s can never be None',
                             erc721_address, safe_address)
                token_id = 0
            erc721_address = erc721_event.address
            contract = get_erc721_contract(self.ethereum_client.w3, erc721_address)
            ownership_queries.append(contract.functions.ownerOf(token_id))
            token_uri_queries.append(contract.functions.tokenURI(token_id))  # More optimal to do it here

        token_uri_filtered_queries = []
        filtered_events = []
        for erc721_event, token_uri_query, owner in zip(erc721_events,
                                                        token_uri_queries,
                                                        self.ethereum_client.batch_call(ownership_queries,
                                                                                        raise_exception=False)):
            if owner != safe_address:  # Leave out tokens the user does not have already
                continue
            token_uri_filtered_queries.append(token_uri_query)
            filtered_events.append(erc721_event)

        for erc721_event, token_uri in zip(filtered_events, self.ethereum_client.batch_call(token_uri_queries,
                                                                                            raise_exception=False)):
            token_id = erc721_event.arguments.get('tokenId')
            erc721_address = erc721_event.address
            token_info = self.get_token_info(erc721_address)
            if not token_info:
                name, symbol = ('', '')
            else:
                name, symbol = token_info
                if (len(name) - len(symbol)) < -5:  # If symbol is way bigger than name, swap them (e.g. POAP)
                    name, symbol = symbol, name
            if not token_uri:
                if erc721_address in self.crypto_kitties_contract_addresses:
                    token_uri = f'https://api.cryptokitties.co/kitties/{token_id}'
                else:
                    logger.warning('Cannot get ERC721 info token=%s with token-id=%d and owner=%s',
                                   erc721_address, token_id, safe_address, exc_info=True)
            collectibles.append(Collectible(name, symbol, erc721_address, token_id, token_uri))
        return collectibles

    @lru_cache
    def _get_metadata(self, uri: str) -> Dict[Any, Any]:
        """
        Get metadata from uri. Maybe at some point support IPFS or another protocols. Currently just http/https is
        supported
        :param uri: Uri starting with the protocol, like http://example.org/token/3
        :return: Metadata as a decoded json
        """
        if not uri or not uri.startswith('http'):
            raise MetadataRetrievalException(uri)

        try:
            response = requests.get(uri)
            if not response.ok:
                raise MetadataRetrievalException(uri)
            else:
                return response.json()
        except requests.RequestException as e:
            raise MetadataRetrievalException(uri) from e

    def get_collectibles_with_metadata(self, safe_address: str) -> List[CollectibleWithMetadata]:
        collectibles_with_metadata = []
        for collectible in self.get_collectibles(safe_address):
            try:
                metadata = self._get_metadata(collectible.uri)
            except MetadataRetrievalException:
                metadata = {}
                logger.warning(f'Cannot retrieve token-uri={collectible.uri} '
                               f'for token-address={collectible.address}')

            collectibles_with_metadata.append(
                CollectibleWithMetadata(collectible.token_name, collectible.token_symbol,
                                        collectible.address, collectible.id, collectible.uri, metadata)
            )
        return collectibles_with_metadata

    @cachedmethod(cache=operator.attrgetter('cache_token_info'))
    def get_token_info(self, token_address: str) -> Tuple[str, str]:
        """
        :param token_address: address for a erc721 token
        :return: tuple with name and symbol of the erc721 token
        """
        if token_address in self.ens_contract_address:  # TODO Refactor custom cases
            return 'Ethereum Name Service', 'ENS'

        try:
            erc_721_contract = get_erc721_contract(self.ethereum_client.w3, token_address)
            name, symbol = self.ethereum_client.batch_call([erc_721_contract.functions.name(),
                                                            erc_721_contract.functions.symbol()])
            return name, symbol
        except ValueError:
            logger.warning('Cannot get erc721 token info for token-address=%s', token_address)
            return None
