"""Bounded editorial layout hotfix, preserving earlier rollback baselines."""
import v126_ui_hotfix7 as hotfix

hotfix.WORK = hotfix.ROOT / 'test-output/v126-hotfix-ui9-20260829'
hotfix.HOTFIX = 'v126-ui-hotfix-20260829-09'
hotfix.HOTFIX_LABEL = '选片布局热修复9'
hotfix.IDENTITY_SCOPE = 'base_release_identity_plus_ui_hotfix9'

if __name__ == '__main__':
    raise SystemExit(hotfix.main())
