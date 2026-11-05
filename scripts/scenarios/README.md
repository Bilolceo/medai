# Live scenario suites (Dr Sultonbek)

Played against the real model on the **local** stack before a release. Each
bad reply found in a real chat becomes a scenario here.

    docker cp scripts/scenarios/. doctorpp-api-1:/app/
    docker compose exec -T -e OPENAI_MODEL=gpt-5.4-mini api sh -c "cd /app && python intake_suite.py"
    docker compose exec -T -e OPENAI_MODEL=gpt-5.4-mini -e N=45 api sh -c "cd /app && python mock400_suite.py"

`intake_suite.py` -- named scenarios (ONLY=<part of a name> to run some).
`mock400_suite.py` -- N random conversations from parts (KIND=libido to force one kind).
`style_check.py` -- the flags both use: filler, literary words, empathy, repeats, length.

Needs a real OPENAI_API_KEY in .env.docker; about $0.002 a reply.
