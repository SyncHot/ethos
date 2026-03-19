import unittest
import sys
import os
import yaml
from unittest.mock import patch, MagicMock

# Add backend directory to sys.path
sys.path.insert(0, '/opt/ethos/backend')

# Mock dependencies
sys.modules['host'] = MagicMock()
sys.modules['utils'] = MagicMock()
sys.modules['utils'].load_json = MagicMock()
sys.modules['flask'] = MagicMock()

try:
    from blueprints.appstore import _validate_compose_policy
except ImportError:
    sys.path.insert(0, '/opt/ethos/backend')
    from blueprints.appstore import _validate_compose_policy

class TestComposeValidation(unittest.TestCase):

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    def test_safe_compose(self, mock_compose_root, mock_apps_root):
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'
        
        safe_yaml = """
services:
  app:
    image: nginx
    volumes:
      - ./data:/data
"""
        # Should pass (return None)
        self.assertIsNone(_validate_compose_policy(safe_yaml))

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    def test_privileged_blocked(self, mock_compose_root, mock_apps_root):
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'

        unsafe_yaml = """
services:
  app:
    image: nginx
    privileged: true
"""
        result = _validate_compose_policy(unsafe_yaml)
        self.assertIn('privileged=true jest niedozwolone', result)

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    def test_unsafe_bind_blocked(self, mock_compose_root, mock_apps_root):
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'

        unsafe_yaml = """
services:
  app:
    image: nginx
    volumes:
      - /:/host
"""
        result = _validate_compose_policy(unsafe_yaml)
        self.assertIsNotNone(result)
        # Should hit sensitive path check for /
        self.assertTrue(
            'sciezki systemowej' in result or 'jest poza dozwolonym obszarem' in result,
            f'Expected bind block error, got: {result}'
        )

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    @patch('blueprints.appstore.os.path.realpath')
    def test_docker_sock_blocked(self, mock_realpath, mock_compose_root, mock_apps_root):
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'
        
        # Ensure consistent behavior for test
        mock_realpath.side_effect = lambda p: p

        unsafe_yaml = """
services:
  app:
    image: nginx
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
"""
        result = _validate_compose_policy(unsafe_yaml)
        self.assertIn('Montowanie docker.sock jest niedozwolone', result)

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    def test_userns_mode_blocked(self, mock_compose_root, mock_apps_root):
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'

        unsafe_yaml = """
services:
  app:
    image: nginx
    userns_mode: host
"""
        result = _validate_compose_policy(unsafe_yaml)
        self.assertIsNotNone(result)
        self.assertIn('userns_mode=host jest niedozwolone', result)
        self.assertIn('zresetuj compose', result)

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    def test_cgroup_parent_blocked(self, mock_compose_root, mock_apps_root):
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'

        unsafe_yaml = """
services:
  app:
    image: nginx
    cgroup_parent: /sys/fs/cgroup
"""
        result = _validate_compose_policy(unsafe_yaml)
        self.assertIsNotNone(result)
        self.assertIn('cgroup_parent jest niedozwolone', result)
        self.assertIn('zresetuj compose', result)

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    def test_security_opt_blocked(self, mock_compose_root, mock_apps_root):
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'

        # Unsafe option blocked
        unsafe_yaml = """
services:
  app:
    image: nginx
    security_opt:
      - seccomp:unconfined
"""
        result = _validate_compose_policy(unsafe_yaml)
        self.assertIsNotNone(result)
        self.assertIn('security_opt "seccomp:unconfined" jest niedozwolone', result)
        self.assertIn('zresetuj compose', result)

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    def test_safe_security_opt_allowed(self, mock_compose_root, mock_apps_root):
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'

        # Safe option allowed
        safe_yaml = """
services:
  app:
    image: nginx
    security_opt:
      - no-new-privileges:true
"""
        result = _validate_compose_policy(safe_yaml)
        self.assertIsNone(result)

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    @patch('blueprints.appstore.os.path.realpath')
    def test_explicit_system_paths(self, mock_realpath, mock_compose_root, mock_apps_root):
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'
        mock_realpath.side_effect = lambda p: p
        
        paths = ['/proc', '/sys', '/boot', '/dev', '/etc', '/usr']
        for p in paths:
            unsafe_yaml = f"""
services:
  app:
    image: nginx
    volumes:
      - {p}:/host_mount
"""
            result = _validate_compose_policy(unsafe_yaml)
            self.assertIsNotNone(result)
            self.assertIn(f'Montowanie sciezki systemowej "{p}" jest zabronione', result)


class TestAdaptComposeStripping(unittest.TestCase):
    """Verify _adapt_compose auto-strips the new unsafe options."""

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    def test_security_opt_filtered(self, mock_compose_root, mock_apps_root):
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'
        from blueprints.appstore import _adapt_compose

        yaml_in = """
services:
  app:
    image: nginx
    security_opt:
      - no-new-privileges:true
      - seccomp:unconfined
"""
        adapted, warnings = _adapt_compose(yaml_in, 'testapp')
        data = yaml.safe_load(adapted)
        
        # Safe option preserved
        self.assertIn('security_opt', data['services']['app'])
        self.assertEqual(data['services']['app']['security_opt'], ['no-new-privileges:true'])
        
        # Unsafe removed and warned
        self.assertTrue(any('security_opt' in w for w in warnings))

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    def test_cgroup_parent_stripped(self, mock_compose_root, mock_apps_root):
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'
        from blueprints.appstore import _adapt_compose

        yaml_in = """
services:
  app:
    image: nginx
    cgroup_parent: /sys/fs/cgroup/system.slice
"""
        adapted, warnings = _adapt_compose(yaml_in, 'testapp')
        data = yaml.safe_load(adapted)
        self.assertNotIn('cgroup_parent', data['services']['app'])
        self.assertTrue(any('cgroup_parent' in w for w in warnings))

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    def test_userns_mode_host_stripped(self, mock_compose_root, mock_apps_root):
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'
        from blueprints.appstore import _adapt_compose

        yaml_in = """
services:
  app:
    image: nginx
    userns_mode: host
"""
        adapted, warnings = _adapt_compose(yaml_in, 'testapp')
        data = yaml.safe_load(adapted)
        self.assertNotIn('userns_mode', data['services']['app'])
        self.assertTrue(any('userns_mode' in w for w in warnings))

    @patch('blueprints.appstore._apps_root')
    @patch('blueprints.appstore._compose_root')
    def test_adapt_then_validate_passes(self, mock_compose_root, mock_apps_root):
        """After _adapt_compose strips unsafe keys, _validate_compose_policy must pass."""
        mock_apps_root.return_value = '/tmp/ethos/apps/_appdata'
        mock_compose_root.return_value = '/tmp/ethos/apps/compose'
        from blueprints.appstore import _adapt_compose

        yaml_in = """
services:
  app:
    image: nginx
    security_opt:
      - no-new-privileges:true
    cgroup_parent: /sys/fs/cgroup/system.slice
    userns_mode: host
"""
        adapted, warnings = _adapt_compose(yaml_in, 'testapp')
        
        # Check warnings for stripped unsafe items
        self.assertTrue(any('cgroup_parent' in w for w in warnings))
        self.assertTrue(any('userns_mode' in w for w in warnings))
        # security_opt was safe, should NOT be warned
        self.assertFalse(any('security_opt' in w for w in warnings))

        result = _validate_compose_policy(adapted)
        self.assertIsNone(result, f'Validation should pass after adapt, got: {result}')


if __name__ == '__main__':
    unittest.main()
