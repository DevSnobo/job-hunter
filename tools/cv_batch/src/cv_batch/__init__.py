"""Batch tailoring of one-page CVs for the job postings in ``applications/``.

The package is intentionally small and dependency-free (standard library only).
Layout:

* :mod:`cv_batch.apps`      - scan ``applications/`` and classify CV targets
* :mod:`cv_batch.selection` - parse the ``1,3-5,12`` selection syntax
* :mod:`cv_batch.catalog`   - candidate catalog from the reviewed baseline + backlogs
* :mod:`cv_batch.prompt`    - bounded prompt construction
* :mod:`cv_batch.model`     - prime-agent invocation, output parsing, failure classes
* :mod:`cv_batch.build`     - validate the model selection, build the cvgen JSON
* :mod:`cv_batch.render`    - invoke the existing CV generator, place the PDF safely
* :mod:`cv_batch.runner`    - the per-job pipeline and batch execution
* :mod:`cv_batch.tty_input` - coloured single-line prompt (shared `answer` style)
* :mod:`cv_batch.cli`       - interactive/non-interactive command line
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
