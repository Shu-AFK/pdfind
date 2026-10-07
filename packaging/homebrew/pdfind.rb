# Formula for a personal tap:
#   brew tap Shu-AFK/tap && brew install pdfind
#
# UNTESTED. It was written without access to a Mac. If it misbehaves, the
# supported macOS route is plain pipx, which needs no formula at all:
#
#   brew install pipx poppler
#   pipx install pdfind
#
# Note this installs dependencies with pip at build time rather than declaring
# 29 `resource` stanzas. That is fine for a personal tap but would be rejected
# by homebrew-core, which requires every dependency pinned. To convert it, run
# `brew update-python-resources Formula/pdfind.rb` on a Mac and swap the
# pip_install call for `virtualenv_install_with_resources`.
class Pdfind < Formula
  include Language::Python::Virtualenv

  desc "Find documents by describing what is written in them"
  homepage "https://github.com/Shu-AFK/pdfind"
  url "https://github.com/Shu-AFK/pdfind/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_TARBALL_SHA256"
  license "MIT"

  depends_on "poppler"      # pdftotext, pdftoppm
  depends_on "python@3.13"

  # Optional, for the `pdfind bar` window:
  #   brew install pygobject3 gtk4
  # Optional, for OCR of scanned PDFs: brew install tesseract
  # Optional, for `pdfind pick`:        brew install fzf

  def install
    venv = virtualenv_create(libexec, "python3.13")
    venv.pip_install buildpath
    bin.install_symlink libexec/"bin/pdfind"
    pkgshare.install "scripts/com.pdfind.daemon.plist",
                     "scripts/com.pdfind.index.plist"
  end

  service do
    run [opt_bin/"pdfind", "serve"]
    keep_alive true
    log_path var/"log/pdfind.log"
    error_log_path var/"log/pdfind.log"
  end

  test do
    assert_match "pdfind", shell_output("#{bin}/pdfind --help")
    system bin/"pdfind", "doctor"
  end
end
