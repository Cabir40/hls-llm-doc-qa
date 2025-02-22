# Databricks notebook source
# MAGIC %md This notebook is available at https://github.com/databricks-industry-solutions/hls-llm-doc-qa

# COMMAND ----------

# MAGIC %md
# MAGIC ## Document Ingestion and Preparation
# MAGIC
# MAGIC <img style="float: right" width="800px" src="https://raw.githubusercontent.com/databricks-industry-solutions/hls-llm-doc-qa/basic-qa-LLM-HLS/images/data-prep.jpeg?token=GHSAT0AAAAAACBNXSB4IK2XJS37QU6HCJCEZEBL3TA">
# MAGIC
# MAGIC
# MAGIC #
# MAGIC 1. Organize your documents into a directory on DBFS or S3 (DBFS is easier but S3 works too)
# MAGIC     * In this demo we have preuploaded a set of PDFs from PubMed on S3, but your own documents will work the same way
# MAGIC 2. Use LangChain to ingest those documents and split them into manageable chunks using a text splitter
# MAGIC 3. Use a sentence transformer NLP model to create embeddings of those text chunks and store them in a vectorstore
# MAGIC     * Embeddings are basically creating a high-dimension vector encoding the semantic meaning of a chunk of text
# MAGIC

# COMMAND ----------

# MAGIC %md
# MAGIC Start with required Python libraries for data preparation.

# COMMAND ----------

# MAGIC %run ./util/install-prep-libraries

# COMMAND ----------

# MAGIC %md
# MAGIC Creating a dropdown widget for model selection, as well as defining the file paths where our PDFs are stored, where we want to cache the Spark NLP model downloads, and where we want to persist our vectorstore.

# COMMAND ----------

# where you want the PDFs to be saved in your environment
dbutils.widgets.text("PDF_Path", "/dbfs/tmp/langchain_hls/pdfs")

# which embeddings model from Spark NLP
dbutils.widgets.text("Embeddings_Model", "en.embed_sentence.bert_base_uncased")

# where you want the vectorstore to be persisted across sessions, so that you don't have to regenerate
dbutils.widgets.text("Vectorstore_Persist_Path", "/dbfs/tmp/langchain_hls/db")

# publicly accessible bucket with PDFs for this demo
dbutils.widgets.text("Source_Documents", "s3a://db-gtm-industry-solutions/data/hls/llm_qa/")

# COMMAND ----------

#get widget values
pdf_path = dbutils.widgets.get("PDF_Path")
source_pdfs = dbutils.widgets.get("Source_Documents")
db_persist_path = dbutils.widgets.get("Vectorstore_Persist_Path")
embeddings_model = dbutils.widgets.get("Embeddings_Model")

# COMMAND ----------

import os
# Optional, but helpful to avoid re-downloading the weights repeatedly. Set to any `/dbfs` path.
#os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:512"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Data Prep
# MAGIC
# MAGIC This data preparation need only happen one time to create data sets that can then be reused in later sections without re-running this part.
# MAGIC
# MAGIC - Grab the set of PDFs (ex: Arxiv papers allow curl, PubMed does not)
# MAGIC - We have are providing a set of PDFs from PubMedCentral relating to Cystic Fibrosis (all from [PubMedCentral Open Access](https://www.ncbi.nlm.nih.gov/pmc/tools/openftlist/), all with the CC BY license), but any topic area would work
# MAGIC - If you already have a repository of PDFs then you can skip this step, just organize them all in an accessible DBFS location

# COMMAND ----------

import os
import shutil

# in case you rerun this notebook, this deletes the directory and recreates it to prevent file duplication
if os.path.exists(pdf_path):
  shutil.rmtree(pdf_path, ignore_errors=True)
os.makedirs(pdf_path)

# slightly modifying the file path from above to work with the dbutils.fs syntax
modified_pdf_path = "dbfs:/" + pdf_path.lstrip("/dbfs")
dbutils.fs.cp(source_pdfs, modified_pdf_path, True)

# COMMAND ----------

# MAGIC %md
# MAGIC All of the PDFs should now be accessible in the `pdf_path` now; you can run the below command to check if you want.
# MAGIC
# MAGIC `!ls {pdf_path}`

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prepare Document DB
# MAGIC
# MAGIC Now it's time to load the texts that have been generated, and create a searchable database of text for use in the `langchain` pipeline. 
# MAGIC These documents are embedded, so that later queries can be embedded too, and matched to relevant text chunks by embedding.
# MAGIC
# MAGIC - Use `langchain` to reading directly from PDFs, although LangChain also supports txt, HTML, Word docs, GDrive, PDFs, etc.
# MAGIC - Create a simple in-memory Chroma vector DB for storage
# MAGIC - Instantiate an embedding function from `sentence-transformers`
# MAGIC - Populate the database and save it

# COMMAND ----------

# MAGIC %md
# MAGIC Prepare a directory to store the document database. Any path on `/dbfs` will do.

# COMMAND ----------

!(rm -r {db_persist_path} || true) && mkdir -p {db_persist_path}

# COMMAND ----------

# MAGIC %md
# MAGIC Create the document database:
# MAGIC - Here we are using the `PyPDFDirectoryLoader` loader from LangChain ([docs page](https://python.langchain.com/en/latest/modules/indexes/document_loaders/examples/pdf.html#using-pypdf)) to form `documents`; `langchain` can also form doc collections directly from PDFs, GDrive files, etc.

# COMMAND ----------

from langchain.docstore.document import Document
from langchain.document_loaders import PyPDFDirectoryLoader

loader_path = f"{pdf_path}/"

pdf_loader = PyPDFDirectoryLoader(loader_path)
docs = pdf_loader.load()
len(docs)

# COMMAND ----------

# MAGIC %md
# MAGIC Here we are using a text splitter from LangChain to split our PDFs into manageable chunks. This is for a few reasons, primarily:
# MAGIC - LLMs (currently) have a limited context length. MPT-7b-Instruct by default can only accept 2048 tokens (roughly words) in the prompt, although it can accept 4096 with a small settings change. This is rapidly changing, though, so keep an eye on it.
# MAGIC - When we create embeddings for these documents, an NLP model (sentence transformer) creates a numerical representation (a high-dimensional vector) of that chunk of text that captures the semantic meaning of what is being embedded. If we were to embed large documents, the NLP model would need to capture the meaning of the entire document in one vector; by splitting the document, we can capture the meaning of chunks throughout that document and retrieve only what is most relevant.
# MAGIC - In this case, the embeddings model we use can except a very limited number of tokens. The default one we have selected in this notebook, [bert_base_uncased](https://sparknlp.org/2020/08/25/bert_base_uncased.html)
# MAGIC - More info on embeddings: [Spark NLP: Embeddings](https://nlp.johnsnowlabs.com/models?task=Embeddings)

# COMMAND ----------

# For PDFs we need to split them for embedding:
from johnsnowlabs import nlp

def split_hay_doc(pipe,doc):
  from  langchain.schema.document import Document
  return [Document(page_content=split, metadata = doc.metadata) for split in pipe.annotate(doc.page_content)['splits'] ]

def split_hay_docs(pipe, docs):
    return [split for doc in docs for split in split_hay_doc(pipe, doc)]


class JslCharSplitter():
  def __init__(self,
               chunk_overlap=2,
               chunk_size=20,
               explode_splits=True,
               keep_seperators=True,
               patterns_are_regex=False,
               split_patterns=['\n\n', '\n', ' ', ''],
               trim_whitespace=True):

    spark = nlp.start()

    documentAssembler = nlp.DocumentAssembler()\
        .setInputCol("text")\
        .setOutputCol("document")

    textSplitter = nlp.DocumentCharacterTextSplitter() \
        .setInputCols(["document"]) \
        .setOutputCol("splits") \
        .setChunkSize(chunk_size) \
        .setChunkOverlap(chunk_overlap) \
        .setExplodeSplits(explode_splits)\
        .setPatternsAreRegex(patterns_are_regex)\
        .setSplitPatterns(split_patterns)\
        .setTrimWhitespace(trim_whitespace)
        # .setKeepSeperators(keep_seperators)\

    self.pipe =  nlp.LightPipeline(nlp.PipelineModel(
        stages=[
        documentAssembler,
        textSplitter
        ]))

  def split_documents(self,docs):
    return split_hay_docs(self.pipe, docs)

# COMMAND ----------
# this is splitting into chunks based on a fixed number of tokens
# the embeddings model we use below can take a maximum of 128 tokens (and truncates beyond that) so we keep our chunks at that max size
jsl_splitter = JslCharSplitter(chunk_size=1000, chunk_overlap=0)
texts = jsl_splitter.split_documents(docs)

# COMMAND ----------

display(documents)

# COMMAND ----------

from langchain.vectorstores import Chroma
from johnsnowlabs.llm import embedding_retrieval

embeddings =  embedding_retrieval.JohnSnowLabsLangChainEmbedder(embeddings_model)

sample_query = "What is cystic fibrosis?"
db = Chroma.from_documents(collection_name="hls_docs", documents=documents, embedding=embeddings, persist_directory=db_persist_path)
db.persist()

# COMMAND ----------

# query it
sample_query = "What is cystic fibrosis?"
similar_docs = db.similarity_search(sample_query)

# print results
print(similar_docs[0].page_content)

# COMMAND ----------

display(similar_docs)

# COMMAND ----------
# MAGIC %md
# MAGIC ## Building a `langchain` Chain
# MAGIC
# MAGIC Now we can compose the database with a language model and prompting strategy to make a `langchain` chain that answers questions.
# MAGIC
# MAGIC - Load the Chroma DB
# MAGIC - Instantiate an LLM, like Dolly here, but could be other models or even OpenAI models
# MAGIC - Define how relevant texts are combined with a question into the LLM prompt

# COMMAND ----------

# Start here to load a previously-saved DB
from langchain.vectorstores import Chroma
from johnsnowlabs.llm import embedding_retrieval

db_persist_path = db_persist_path
embeddings =  embedding_retrieval.JohnSnowLabsLangChainEmbedder(embeddings_model)

db = Chroma(collection_name="hls_docs", embedding_function=embeddings, persist_directory=db_persist_path)