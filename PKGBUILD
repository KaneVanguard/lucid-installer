# Maintainer: Kane Vanguard

pkgname=bbqlinux-installer
pkgver=1.2.3
pkgrel=1
pkgdesc="The LucidSystems Installer"
arch=('any')
url="https://github.com/KanVanguard/lucidsystems-installer"
license=('GPL')
depends=('inxi' 'python2' 'qt4' 'python2-pyqt4' 'parted>=3.0' 'python2-pyparted' 'python2-geoip')
replaces=('')

package() {
  cd "$pkgdir"

  install -Dm755 "$srcdir/usr/bin/lucidsystems-installer" usr/bin/lucidsystems-installer

  cp -R "$srcdir/etc" etc
  cp -R "$srcdir/usr/lib" usr/lib
  cp -R "$srcdir/usr/share" usr/share
}
