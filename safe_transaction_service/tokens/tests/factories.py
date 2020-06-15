import factory
from eth_account import Account

from .. import models


class TokenFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Token

    address = factory.LazyFunction(lambda: Account.create().address)
    name = factory.Faker('cryptocurrency_name')
    symbol = factory.Faker('cryptocurrency_code')
    decimals = 18
    logo_uri = ''
