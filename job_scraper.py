import csv
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ollama
import pandas as pd
from jobspy import scrape_jobs
from pypdf import PdfReader

BASE = Path(__file__).parent
JOBS_CSV = BASE / 'jobs.csv'
# The one PDF in agent_dir - see main.py. Keeps a personal filename out of the repo.
RESUME_PDF = next(iter(sorted((BASE / 'agent_dir').glob('*.pdf'))), BASE / 'agent_dir/resume.pdf')

SEARCH_TERMS = [
    'data analyst',
    'data engineer',
    'Business Intelligence Analyst',
    'Analytics Engineer'
]
LOCATION = 'United States'
RESULTS_PER_TERM = 100
HOURS_OLD = 72

MODEL = 'gemma4:cloud'
MIN_RATING = 4
RATE_WORKERS = 8

# Explicit anchors matter: an unanchored "penalise excess years" instruction dragged
# every posting asking 4+ years down to a 3, including ones this resume clearly fits.
PROMPT = """Rate how plausible it is for this candidate to be interviewed for this \
job. Output ONLY an integer 1-10.

10 = experience and skills line up well
7 = solid fit, minor gaps
5 = stretch but worth applying
3 = job needs roughly double the candidate years, or a different field
1 = completely unqualified or unrelated profession

Judge on the years of experience actually required versus the resume, and on skill \
overlap. Do not penalize a job for asking 1-2 years more than the candidate has.

RESUME:
{resume}

JOB: {title}
{description}

Integer:"""


def rate_match(resume: str, title: str, description) -> int | None:
    """1-10 fit score for one posting, or None if the model can't be reached.

    gemma4:cloud ignores Ollama's `format` schema and answers in prose, so brevity is
    forced with num_predict instead and the first integer is taken.
    """
    if not isinstance(description, str):
        return None
    try:
        reply = ollama.chat(
            model=MODEL,
            options={'temperature': 0, 'num_predict': 5},
            messages=[{'role': 'user', 'content': PROMPT.format(
                resume=resume, title=title, description=description[:4000])}],
        )
    except Exception as e:
        print(f'  rating failed: {e}')
        return None
    digits = re.search(r'\d+', reply.message.content or '')
    return int(digits.group()) if digits else None


def scrape():
    """Scrape each search term nationwide and merge into jobs.csv, deduped on job_url.

    Merging instead of overwriting matters: main.py works out what is left to do by
    diffing jobs.csv against progress.csv, so rewriting the file would resurrect
    jobs that were already applied to.

    jobs.csv is created here when absent, so starting a fresh list means deleting it
    and running this module directly - not via main.py, whose pending_jobs() reads
    jobs.csv before reaching the scrape that would create it. Delete jobs.csv alone:
    progress.csv still suppresses anything already applied to. Delete progress.csv
    instead and the agent re-applies to every row in jobs.csv.
    """
    frames = []
    for term in SEARCH_TERMS:
        try:
            df = scrape_jobs(
                site_name=['linkedin'],
                search_term=term,
                location=LOCATION,
                results_wanted=RESULTS_PER_TERM,
                hours_old=HOURS_OLD,
                country_indeed='USA',
                linkedin_fetch_description=True,
            )
            print(f'  {term}: {len(df)}')
            frames.append(df)
        except Exception as e:
            print(f'  {term}: failed - {e}')

    if not frames:
        return 0

    jobs = pd.concat(frames, ignore_index=True)

    resume = '\n'.join(page.extract_text() for page in PdfReader(RESUME_PDF).pages)
    with ThreadPoolExecutor(RATE_WORKERS) as pool:
        jobs['match_rating'] = list(
            pool.map(lambda r: rate_match(resume, r.title, r.description),
                     jobs.itertuples())
        )

    # Unrated jobs (model unreachable, or no description) are kept rather than dropped,
    # so an Ollama outage can't silently empty the queue.
    rated = jobs['match_rating']
    jobs = jobs[rated.isna() | (rated >= MIN_RATING)].drop(columns='description')
    print(f'  kept {len(jobs)} at rating >= {MIN_RATING} ({rated.notna().sum()} rated)')

    if JOBS_CSV.exists():
        jobs = pd.concat([pd.read_csv(JOBS_CSV), jobs], ignore_index=True)

    before = len(jobs)
    jobs = jobs.drop_duplicates(subset='job_url', keep='first')
    jobs.to_csv(JOBS_CSV, quoting=csv.QUOTE_NONNUMERIC, escapechar='\\', index=False)
    print(f'jobs.csv: {len(jobs)} total ({before - len(jobs)} duplicates dropped)')
    return len(jobs)


if __name__ == '__main__':
    scrape()
