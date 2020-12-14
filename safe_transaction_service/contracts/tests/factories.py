import factory
from eth_account import Account
from factory.django import DjangoModelFactory

from .mocks import sourcify_safe_metadata
from ..models import ContractAbi, Contract


class ContractAbiFactory(DjangoModelFactory):
    class Meta:
        model = ContractAbi

    abi = sourcify_safe_metadata['output']['abi']
    description = 'Gnosis Safe v1.2.0 ABI'
    relevance = 1


class ContractFactory(DjangoModelFactory):
    class Meta:
        model = Contract

    address = factory.LazyFunction(lambda: Account.create().address)
    name = factory.Faker('cryptocurrency_name')
    contract_abi = factory.SubFactory(ContractAbiFactory)
