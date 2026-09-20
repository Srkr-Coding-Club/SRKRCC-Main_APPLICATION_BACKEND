from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('codequest', '0002_submission_codequest_s_user_id_f0bf04_idx')]

    operations = [
        migrations.AddField(model_name='problem', name='external_platform', field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name='problem', name='external_url', field=models.URLField(blank=True)),
    ]
