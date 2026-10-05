"""Explicit canonical compiler/runtime composition for retained pure applications.

The selected compiler emits; the selected VM admits and enforces. Forge owns
only process lifetime, receipts and the existing assurance result projection.
"""
from __future__ import annotations
import importlib.util
import sys
import threading
from pathlib import Path


class CanonicalVmApplication:
    def __init__(self, *, compiler_checkout: Path, vm_checkout: Path, executable: Path,
                 cache: Path, compiler_executable: Path | None = None, timeout: float = 120):
        self.compiler_checkout = compiler_checkout.resolve()
        path = self.compiler_checkout / 'tools/vm_provider.py'
        spec = importlib.util.spec_from_file_location('forge_selected_compiler_provider', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.producer = module.CompilerProvider(self.compiler_checkout, executable=compiler_executable, timeout=timeout)
        path = vm_checkout.resolve() / 'python/mncs_vm_client/__init__.py'
        spec = importlib.util.spec_from_file_location('forge_selected_vm_transport', path)
        client = importlib.util.module_from_spec(spec); spec.loader.exec_module(client)
        Session = client.Session
        self.session_type = Session
        self.executable, self.cache, self.timeout = executable.resolve(), cache.resolve(), timeout
        self.session = None
        self.product = None
        self._lock = threading.Lock()

    def call(self, source: Path, request: dict, *, libraries=()):
        if request.get('grants') or request.get('host_grants'):
            raise RuntimeError('canonical pure application has no selected host capability provider')
        compile_request = {'schema_version':'mncs.compiler-vm-request/1', 'source':str(source.resolve()),
                           'logical_name':source.name, 'libraries':[str(Path(p).resolve()) for p in libraries]}
        with self._lock:
            product = self.producer.emit(compile_request, self.cache)
            if self.product is None or self.product['build_receipt']['identity'] != product['build_receipt']['identity']:
                self.close()
            self.product = product
            if self.session is None:
                self.session = self.session_type(self.executable, self.cache / product['artifact']['address'],
                                                 timeout=self.timeout, build_receipt=product['build_receipt'])
            result = self.session.call(request)
            result['execution_provenance']['build'] = product['build_receipt']
            return result

    def status(self):
        return {'selected':'canonical-vm', 'active':self.session is not None,
                'artifact':self.product['artifact'] if self.product else None,
                'producer':self.product['producer']['producer']['identity'] if self.product else None,
                'runtime':self.session.runtime if self.session else None,
                'calls':self.session.count if self.session else 0,
                'cache_reused':self.product['cache_reused'] if self.product else None}

    def close(self):
        if self.session is not None:
            self.session.close()
            self.session = None
