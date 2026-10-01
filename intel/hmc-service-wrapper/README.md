# hmc-service-wrapper

English | [中文](README.zh.md)

This package re-exports /root/intel/hmc-client/service/plugin.mjs without modifying HMC source. cordis.patch.yml registers the wrapper as a DSH bundle; intel/bootstrap.sh installs it through a file: dependency copied by npm into the hmc-test profile. That HMC checkout must exist at the declared absolute path.

## Verification scope

hmc-test provides an API smoke composition. It does not include HMC's full service/cordis.patch.yml with directory-picker replacement, web-runtime configuration and PowerShell sandbox configuration. In particular HMC_PWSH_PATH belongs to a Windows installation. A successful bootstrap proves profile installation only; Host startup and API availability require a separate smoke check. Phone behavior requires the actual paired Host and device acceptance.
