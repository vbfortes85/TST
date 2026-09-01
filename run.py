#!/usr/bin/env python3
"""Inicia o protótipo do agente. Uso: python3 run.py [porta]"""

import os
import sys

from agente import servidor

if __name__ == "__main__":
    porta = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORTA", "8000"))
    servidor.iniciar(porta)
