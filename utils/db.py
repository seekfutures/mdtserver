import uuid


def generate_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:24].upper()}"


def rows_to_dicts(cursor, rows):
    columns = [column[0].lower() for column in cursor.description]
    return [dict(zip(columns, row)) for row in rows]
