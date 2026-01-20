from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_alter_videolesson_duration'),
        ('coin', '0001_initial'),
    ]

    operations = [
        migrations.DeleteModel(
            name='ActivityLog',
        ),
        migrations.DeleteModel(
            name='CoinTransaction',
        ),
        migrations.DeleteModel(
            name='ProductComment',
        ),
        migrations.DeleteModel(
            name='ProductLike',
        ),
        migrations.DeleteModel(
            name='ProductPurchase',
        ),
        migrations.DeleteModel(
            name='Product',
        ),
    ]
