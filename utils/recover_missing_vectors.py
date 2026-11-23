"""Add absent vector records from a verified local Ember archive; dry run by default."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

from psycopg2 import sql

from archive.importers.postgres_importer import PostgresImporter
from memory.postgres import connection


COLUMNS = ('content', 'embedding', 'insight', 'insight_embedding', 'importance', 'confidence',
           'clarity', 'access_count', 'last_accessed', 'metadata', 'time', 'is_consolidated', 'memory_id')


def vector_rows(path):
    with zipfile.ZipFile(path) as archive:
        files = sorted((Path(item.filename), item) for item in archive.infolist() if not item.is_dir())
        if len({name for name, _ in files}) != len(files):
            raise ValueError('Archive contains duplicate paths')
        digest = hashlib.md5()
        for name, item in files:
            if name.name != 'manifest.json':
                digest.update(name.name.encode())
                digest.update(archive.read(item))
        manifest = json.loads(archive.read('manifest.json'))
        if manifest.get('checksum') != digest.hexdigest():
            raise ValueError('Archive integrity check did not pass')
        payload = archive.read('episodic_memory.sql').decode('utf-8')
    parser = PostgresImporter(str(path.parent))
    columns = parser._extract_columns_from_sql(payload)
    if not set(COLUMNS) <= set(columns) or not re.search(r'^INSERT INTO (?:public\.)?episodic_memory \(', payload, re.M):
        raise ValueError('Unexpected vector backup format')
    output = {}
    for raw in parser._extract_values_from_sql(payload):
        values = parser._parse_values(raw)
        if len(columns) != len(values):
            raise ValueError('Vector backup has inconsistent columns')
        row = dict(zip(columns, values))
        key = row['memory_id']
        if not key or key in output or not row['embedding'] or not row['insight_embedding']:
            raise ValueError('Archive contains a missing/duplicate identifier or unset vector')
        output[key] = tuple(row[field] for field in COLUMNS)
    return output


def merge_missing(db, rows, *, apply=False):
    with db.cursor() as cursor:
        if not apply:
            cursor.execute('SET TRANSACTION READ ONLY')
        cursor.execute('SELECT memory_id FROM episodic_memory WHERE memory_id=ANY(%s)', (list(rows),))
        existing = {item[0] for item in cursor.fetchall()}
        pending = [row for key, row in rows.items() if key not in existing]
        inserted = 0
        if apply:
            query = sql.SQL('INSERT INTO episodic_memory ({}) VALUES ({}) ON CONFLICT (memory_id) DO NOTHING').format(
                sql.SQL(',').join(map(sql.Identifier, COLUMNS)),
                sql.SQL(',').join(sql.Placeholder() for _ in COLUMNS))
            for row in pending:
                cursor.execute(query, row)
                inserted += cursor.rowcount
        return {'backup': len(rows), 'already_present': len(existing), 'missing': len(pending), 'inserted': inserted}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expect-missing', type=int, help='Abort the transaction if the reviewed insertion count changed')
    args = parser.parse_args()
    root = Path('data/archives').resolve()
    path = args.archive.resolve(strict=True)
    if path.parent != root or path.suffix != '.ember':
        parser.error('Select a local archive from this runtime’s data/archives directory')
    rows = vector_rows(path)
    with connection() as db:
        result = merge_missing(db, rows, apply=args.apply)
        if args.expect_missing is not None and result['missing'] != args.expect_missing:
            raise SystemExit('Missing vector count changed; transaction rolled back')
        if args.apply and result['inserted'] != result['missing']:
            raise SystemExit('A simultaneous write changed the merge; transaction rolled back')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
