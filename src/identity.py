"""Versioned display normalization and stable content identity."""

import hashlib
import re
import unicodedata

NORMALIZATION_VERSION = 'v1'


def normalize_display(text: str) -> str:
    text = unicodedata.normalize('NFC', text).lstrip('\ufeff')
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\xa0', ' ')
    return '\n'.join(re.sub(r'^[^\S\n]+|[^\S\n]+$', '', line)
                     for line in text.split('\n')).strip('\n')


def canonical_text(text: str) -> str:
    return ' '.join(normalize_display(text).split())


def make_post_id(content: str) -> str:
    return 'post_' + hashlib.sha256(('post:v1\n' + canonical_text(content)).encode('utf-8')).hexdigest()
