.PHONY: setup dev run migrations migrate superuser shell check clean

# Virtual environment python binary
VENV = venv
UV = uv


setup:
	@echo "Creating virtual environment with uv..."
	$(UV) venv $(VENV)
	@echo "Installing dependencies..."
	$(UV) pip install -r requirements.txt
	@echo "Backend environment ready! Update .env with your local PostgreSQL credentials if needed."

dev: run

run:
	$(UV) run python manage.py runserver 8000

migrations:
	$(UV) run python manage.py makemigrations

migrate:
	$(UV) run python manage.py migrate

superuser:
	$(UV) run python manage.py createsuperuser

shell:
	$(UV) run python manage.py shell

check:
	$(UV) run python manage.py check

clean:
	rm -rf __pycache__ .pytest_cache
	find . -type d -name "__pycache__" -exec rm -rf {} +
