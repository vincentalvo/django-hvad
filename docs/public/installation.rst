############
Installation
############


************
Requirements
************

* `Django`_ 1.3 or higher. Django 1.6 or higher is recommended.
* Python 2.6 or a higher release of Python 2.x or PyPy 1.5 or higher, Python 3.3 or higher.
* For Python 2.6 you need `argparse`_

************
Installation
************

Packaged version
================

.. highlight:: console

This is the recommended way. Install django-hvad using `pip`_ by running::

    pip install django-hvad

This will download the latest version from `pypi`_ and install it.

Then add ``'hvad'`` to your :std:setting:`INSTALLED_APPS`, and proceed to
:doc:`quickstart`.

Latest development version
==========================

.. highlight:: console

If you need the latest features from a yet unreleased version, or just like
living on the edge, install django-hvad using `pip`_ by running::

    pip install https://github.com/kristianoellegaard/django-hvad/tarball/master

This will download the development branch from `github`_ and install it.

Then add ``'hvad'`` to your :std:setting:`INSTALLED_APPS`, and proceed to
:doc:`quickstart`.

.. _pip: http://pypi.python.org/pypi/pip
.. _pypi: https://pypi.python.org/pypi/django-hvad
.. _github: https://github.com/kristianoellegaard/django-hvad
.. _Django: http://www.djangoproject.com
.. _django-cbv: http://pypi.python.org/pypi/django-cbv
.. _argparse: http://pypi.python.org/pypi/argparse
