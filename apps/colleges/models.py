from django.db import models

from apps.colleges.text import normalize_search_text

DEFAULT_COLLEGE_CODE = 'C-24163'
DEFAULT_COLLEGE_NAME = 'SRKR Engineering College'

# Extra names a college is searched by. The catalogue only carries the acronym
# for the club's home college, but members type the founder's full name.
SEARCH_ALIASES = {
    DEFAULT_COLLEGE_CODE: 'Sagi Rama Krishnam Raju Engineering College',
}


def build_search_name(code: str, name: str) -> str:
    return normalize_search_text(f"{name} {SEARCH_ALIASES.get(code, '')}")


class College(models.Model):
    code = models.CharField(max_length=20, unique=True, help_text="Source catalogue ID, e.g. C-24163.")
    name = models.CharField(max_length=300)
    search_name = models.CharField(max_length=300, db_index=True, editable=False)
    university = models.CharField(max_length=300, blank=True)
    college_type = models.CharField(max_length=60, blank=True)
    state = models.CharField(max_length=80, blank=True, db_index=True)
    district = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ['name', 'id']

    def save(self, *args, **kwargs):
        self.search_name = build_search_name(self.code, self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


def get_default_college() -> College:
    return College.objects.get(code=DEFAULT_COLLEGE_CODE)
