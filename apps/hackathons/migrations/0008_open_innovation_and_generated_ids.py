from django.db import migrations, models


class Migration(migrations.Migration):
    """Problem statements get a `domain` (renamed from `category`) and an
    app-generated `code`; teams may bring their own problem (open innovation)."""

    dependencies = [
        ('hackathons', '0007_team_problem_statement_set_null'),
    ]

    operations = [
        migrations.RenameField(model_name='problemstatement', old_name='category', new_name='domain'),
        migrations.AlterField(
            model_name='problemstatement', name='code',
            field=models.CharField(blank=True, max_length=30),
        ),
        migrations.AddField(
            model_name='hackathon', name='allow_open_innovation',
            field=models.BooleanField(
                default=True,
                help_text='Let teams bring their own problem (title, description, domain) instead of picking an admin-defined statement.',
            ),
        ),
        migrations.AddField(model_name='team', name='is_open_innovation', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='team', name='custom_problem_title', field=models.CharField(blank=True, max_length=255)),
        migrations.AddField(model_name='team', name='custom_problem_description', field=models.TextField(blank=True)),
        migrations.AddField(model_name='team', name='custom_problem_domain', field=models.CharField(blank=True, max_length=100)),
    ]
