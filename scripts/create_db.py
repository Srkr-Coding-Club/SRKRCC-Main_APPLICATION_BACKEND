import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
import os
from dotenv import load_dotenv

load_dotenv()

def create_database():
    # Attempt connecting to default postgres database to create srkrcc_db
    user = os.getenv('DB_USER', 'postgres')
    password = os.getenv('DB_PASSWORD', 'postgres')
    host = os.getenv('DB_HOST', 'localhost')
    port = os.getenv('DB_PORT', '5432')
    db_name = os.getenv('DB_NAME', 'srkrcc_db')

    try:
        con = psycopg2.connect(dbname='postgres', user=user, password=password, host=host, port=port)
        con.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = con.cursor()
        
        cur.execute(f"SELECT 1 FROM pg_catalog.pg_database WHERE datname = '{db_name}';")
        exists = cur.fetchone()
        if not exists:
            cur.execute(f'CREATE DATABASE {db_name};')
            print(f"Successfully created database '{db_name}' in local PostgreSQL!")
        else:
            print(f"Database '{db_name}' already exists in local PostgreSQL.")
        
        cur.close()
        con.close()
    except Exception as e:
        print(f"Note: Could not auto-create database '{db_name}': {e}")
        print("Please ensure PostgreSQL is running and create 'srkrcc_db' in pgAdmin if needed.")

if __name__ == '__main__':
    create_database()
