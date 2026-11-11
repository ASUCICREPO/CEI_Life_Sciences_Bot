FROM public.ecr.aws/lambda/python:3.12
COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt
# Only the local HGVS grammar/parser is used. Its optional reference-data stack
# pulls native PostgreSQL and sequence repository dependencies into the image.
RUN pip install --no-cache-dir --no-deps hgvs==1.5.6
COPY handler.py sequence_basics.py protein_primer.py chemistry.py lab_calculations.py ${LAMBDA_TASK_ROOT}/
CMD ["handler.handler"]
