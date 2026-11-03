"""Constrain only RAW_MEDIA headings/order, never invent or repair field values.

Used on one declared bounded schema retry. The normal quality validator still
rejects empty, truncated, repetitive, or semantically invalid output.
"""
from qwenvl_raw_media_v3 import GROUPS, KEYWORD_MIN
import re

class OrderedFieldsProcessor:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        prefixes = []
        for heading, fields in GROUPS:
            for number, field in enumerate(fields):
                prefixes.append((heading+'\n' if number == 0 else '')+field+'：')
        self.prefixes = [tokenizer.encode(p, add_special_tokens=False) for p in prefixes]
        self.newline = tokenizer.encode('\n', add_special_tokens=False)
        if len(self.newline) != 1:
            raise ValueError('schema_constraint_requires_single_newline_token')
        self.eos = tokenizer.eos_token_id
        self.base = None
        self.field = 0
        self.position = 0
        self.seen = 0
        self.has_value = False
        self.mask = None
        self.value_tokens = []

    def __call__(self, tokens, logits):
        import mlx.core as mx
        ids = tokens.tolist()
        if self.base is None:
            self.base = len(ids)  # first call includes the input prompt
        generated = ids[self.base:]
        for token in generated[self.seen:]:
            prefix = self.prefixes[self.field]
            if self.position < len(prefix):
                if token != prefix[self.position]:
                    raise ValueError('schema_constraint_prefix_mismatch')
                self.position += 1
            elif token == self.newline[0] and self.field < len(self.prefixes)-1:
                self.field += 1
                self.position = 0
                self.has_value = False
                self.value_tokens = []
            else:
                self.has_value = True
                self.value_tokens.append(token)
        self.seen = len(generated)
        prefix = self.prefixes[self.field]
        if self.position < len(prefix):
            # A forced label should not inherit repetition penalties on labels.
            mask = mx.full(logits.shape, -float('inf'))
            mask[..., prefix[self.position]] = 0.0
            return mask
        if self.mask is None:
            forbidden = []
            for i in range(logits.shape[-1]):
                piece = self.tokenizer.decode([i])
                if ('\n' in piece or '\r' in piece) and i != self.newline[0]:
                    forbidden.append(i)
            self.mask = mx.zeros(logits.shape)
            self.mask[..., forbidden] = -float('inf')
        result = logits + self.mask
        if self.field < len(self.prefixes)-1:
            result[..., self.eos] = -float('inf')
        if not self.has_value:
            result[..., self.newline[0]] = -float('inf')
            result[..., self.eos] = -float('inf')
        elif self.field == len(self.prefixes)-1:
            result[..., self.newline[0]] = -float('inf')
        if self.field in (9,10) and self.has_value:
            value = self.tokenizer.decode(self.value_tokens).strip()
            minimum = KEYWORD_MIN if self.field == 9 else 3
            terms = [t for t in re.split('[、,，;；]',value) if t.strip(' 。.!！?？')]
            if value != '无法可靠判断' and len(terms) < minimum:
                result[..., self.newline[0]] = -float('inf')
        return result
