The shotgun event daemon listens to all shotgun events, and distributes them to one or more plugin event handlers. These may register for a particular kind of event to only receive the ones they are interested in.

Thanks to the daemon, you may focus on what you are interested in: handling an event, without dealing with details such as:

* repeat events previously missed (due to system failure, for instance)
* maintain a shotgun connection and reconnect automatically
* run as system service


![under construction](https://raw.githubusercontent.com/Byron/bcore/master/src/images/wip.png)


Requirements
============

* python 2.6 or 2.7
* [bcore](https://github.com/Byron/bcore)
* a posix compatible platform (e.g. linux, osx) if you want to use the built-in daemonization

Installation
============

Please head over to the [distribution](https://github.com/Byron/shotgun-events) for installation instructions.

Infrastructure
===============

* **[Continuous Integration/Quality Assurance](https://travis-ci.org/Byron/shotgun-events)**
* **[Issue Tracker](https://github.com/Byron/shotgun-events/issues)**
* **[Documentation](http://byron.github.io/shotgun-events)**

License
=======

This software is provided under the MIT License that can be found in the LICENSE
file or here: <http://www.opensource.org/licenses/mit-license.php>