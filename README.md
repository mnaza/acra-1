<h3 align="center">
  <a href="https://www.cossacklabs.com"><img src="https://github.com/cossacklabs/acra/wiki/Images/acra_web.jpg" alt="Acra: transparent database encryption server" width="500"></a>
  <br>
  Database protection suite with selective encryption and intrusion detection.
  <br>
</h3>


![CircleCI](https://circleci.com/gh/cossacklabs/acra/tree/master.svg?style=shield)
![Go Report Card](https://goreportcard.com/badge/github.com/cossacklabs/acra)

**[Documentation](https://github.com/cossacklabs/acra/wiki) // [Python sample project](https://github.com/cossacklabs/djangoproject.com) // [Ruby sample project](https://github.com/cossacklabs/rubygems.org)**

## What is Acra

Acra helps you to easily secure your databases in distributed, microservice-rich environments. It allows you to selectively encrypt sensitive records with [strong multi-layer cryptography](https://github.com/cossacklabs/acra/wiki/AcraStruct), detect potential intrusions and SQL injections and cryptographically compartment data stored in large sharded schemes. It's security model guarantees that compromising the database or your application does not leak sensitive data, or keys to decrypt it. 

Acra gives you means to encrypt the data on application side into a special cryptographic container, store it in the database and then decrypt in secure compartmented area (separate virtual machine/container). Cryptographic design ensures that no secret (password, key, anything) leaked from the application or database is sufficient to decrypt protected data chunks which originate from it. 

Acra was built with specific user experiences in mind: 
- **quick and easy integration** of security instrumentation.
- cryptographically protect data in threat model, where **all other parts of infrastructure could be compromised**, and if AcraServer isn't, data is safe. 
- **proper abstraction** of all cryptographic processes: you don't risk mischoosing key length or algorithm padding. 
- **strong default settings** to get you going. 
- **intrusion detection** to let you know early that something wrong is going on.
- **high degree of configurability** to create perfect balance between extra security features and performance. 
- **automation-friendly**: most of Acra's features were built to be easily configured / automated from configuration automation environment.
- **limited attack surface**: to compromise Acra-powered app, attacker will need to compromise separate compartmented server, AcraServer, more specifically it's key storage, and the database. 

## Cryptography

Acra relies on our cryptographic library [Themis](https://www.github.com/cossacklabs/themis), which implements high-level cryptosystems based on best availble [open-source implementations](https://github.com/cossacklabs/themis/wiki/Cryptographic-donors) of [most reliable ciphers](https://github.com/cossacklabs/themis/wiki/Soter). Acra does not contain any self-made cryptographic primitives or obscure ciphers, instead, to deliver it's unique guarantees, Acra relies on combination of well-known ciphers and smart key management scheme.

## Availability

* Acra source builds with Go versions 1.2.2, 1.3, 1.3.3, 1.4, 1.4.3, 1.5, 1.5.4, 1.6, 1.6.4, 1.7, 1.7.5, 1.8.
* Acra is known to build on: Debian jessie x86_64, Debian jessie i686, CentOS 7(1611) x86_64, CentOS 6.8 i386.
* Acra currently supports PostgreSQL 9.4+ as the database backend, MongoDB and MariaDB (and other MySQL flavours) coming quite soon. 
* Acra has writer libraries for Ruby, Python, Go and PHP, but you can easily [generate AcraStruct containers](https://github.com/cossacklabs/acra/wiki/AcraStruct)  with [Themis](https://github.com/cossacklabs/themis) for any other platform you desire. 

## How Acra works?

<p align="center"><img src="https://github.com/cossacklabs/acra/wiki/Images/simplified_arch.png" alt="Acra: simplified architecture" width="500"></p>

After successfully deploying and integrating Acra into your application (see below, 4 steps to start):

* Your app talks to **AcraProxy**, local daemon, via PostgreSQL driver. **AcraProxy**  emulates your normal PostgreSQL database, forwards all requests to **AcraServer** over secure channel and expects back plaintext output to return, then forwarding it over the initial PostgreSQL connection to application. It is connected to **AcraServer** via [Secure Session](https://github.com/cossacklabs/themis/wiki/Secure-Session-cryptosystem), ensuring that all plaintext goes over protected channel. It is highly desirable to run AcraProxy via separate user to compartment it from client-facing code. 
* **AcraServer** is a core entity, providing decryption services for all encrypted envelopes coming from database and then re-packing database answers for the application.
* To write protected data to database, you can use **AcraWriter library**, which generates AcraStructs and helps you  integrate it as a type into your ORM or database management code. You will need Acra's public key to do that. AcraStructs generated by AcraWriter are not readable by it - only server has sufficient keys to decrypt it. 
* You can connect to both **AcraProxy** and directly to database when you don't need encrypted reads/writes. However, increased performance might cost design elegance (which is sometimes perfectly fine, when it's conscious choice).

To better understand architecture and data flow, please refer to [Architecture and data flow](https://github.com/cossacklabs/acra/wiki/Architecture-and-data-flow) from official documentation.

Typical flow looks like this: 
- App encrypts some records using AcraWriter, generating AcraStruct with AcraServer public key, updates database. 
- App sends SQL request through AcraProxy, which forwards it to AcraServer, AcraServer forwards it to database. 
- Upon receiving an answer, AcraServer tries to detect encrypted envelopes (AcraStructs), and, if succeeded, decrypts payload and replacing them with plaintext answer, which then gets returned to AcraProxy over secure channel. 
- AcraProxy then provides answer to application, as if no complex security instrumentation is ever present within the system.

## 4 steps to start

* Read the wiki page on [building and installing](https://github.com/cossacklabs/acra/wiki/Quick-start-guide)  all components. Soon enough, they'll be available as pre-built binaries, but for now you'll need to fire a few commands to get the binaries going. 
* [Deploy AcraServer](https://github.com/cossacklabs/acra/wiki/Quick-start-guide) binaries in separate virtual machine ( pr [try it in a docker container](https://github.com/cossacklabs/acra/wiki/Trying-Acra-with-Docker)). Generate keys, put AcraServer public key into both clients (AcraProxy and AcraWriter, see next).
* Deploy [AcraProxy](https://github.com/cossacklabs/acra/wiki/AcraProxy-and-AcraWriter#acraproxy) on each server you need to read sensitive data. Generate proxy keys, provide public one to AcraServer. Point your database access code to AcraProxy, access it as if it's your normal database installation!
* Integrate [AcraWriter](https://github.com/cossacklabs/acra/wiki/AcraProxy-and-AcraWriter#acrawriter) into your code where you need to store sensitive data, supply AcraWriter with proper server key.

## Additionally

We fill [wiki](https://github.com/cossacklabs/acra/wiki) with useful reads on core Acra concepts, use cases, details on cryptographic and security design. You might want to:
- Read notes on [security design](https://github.com/cossacklabs/acra/wiki/Security-design) to understand better what you get by using Acra and what is the threat model Acra operates in. 
- Read [some notes on making Acra stronger / more performant](https://github.com/cossacklabs/acra/wiki/Tuning-Acra), adding security features or increasing throughput, depending on your goals and security model.

## Project status

This open source version of Acra is early alpha. We're slowly unifying and moving features from it's previous incarnation into community-friendly edition. Please let us know in the [Issues](https://www.github.com/cossacklabs/acra/issues) whenever you stumble upon a bug, see a possible enhancement or comment on security design.

## License

Acra is licensed as Apache 2 open source software.

