"""Local token estimation.

Only used for reporting and budgeting the digest; real consumption is always
read from the API usage recorded in transcripts (see report.py). The ratio is
a conservative average for mixed English, code and logs.
"""

CHARS_PER_TOKEN = 3.6


def estimate(text):
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN) + 1
