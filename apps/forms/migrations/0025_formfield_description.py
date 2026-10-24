from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('forms', '0024_alter_formfield_profile_field'),
    ]

    operations = [
        migrations.AddField(
            model_name='formfield',
            name='description',
            field=models.TextField(blank=True, default='', help_text='Help text shown under the question label.'),
        ),
    ]
