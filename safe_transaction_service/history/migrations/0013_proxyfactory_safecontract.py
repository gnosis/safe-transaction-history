# Generated by Django 2.2.5 on 2019-09-25 09:07

from django.db import migrations, models
import django.db.models.deletion
import gnosis.eth.django.models


class Migration(migrations.Migration):

    dependencies = [
        ('history', '0012_auto_20190919_1458'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProxyFactory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('address', gnosis.eth.django.models.EthereumAddressField()),
                ('initial_block_number', models.IntegerField(default=0)),
                ('index_block_number', models.IntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(
            name='SafeContract',
            fields=[
                ('address', gnosis.eth.django.models.EthereumAddressField(primary_key=True, serialize=False)),
                ('ethereum_tx', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='safe_contracts', to='history.EthereumTx')),
            ],
        ),
    ]