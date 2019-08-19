# Generated by Django 2.2.4 on 2019-08-16 08:17

from django.db import migrations

import gnosis.eth.django.models


class Migration(migrations.Migration):

    dependencies = [
        ('history', '0004_ethereumtx_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='ethereumblock',
            name='parent_hash',
            field=gnosis.eth.django.models.Sha3HashField(default=None, unique=True),
            preserve_default=False,
        ),
    ]
