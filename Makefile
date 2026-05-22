all: release

clean:
	@echo Cleaning...
	-rm -r ./build
	-rm -r ./dist

remove_symlinks:
	@echo Removing symlinks for build...
	-rm ./LXMF
	-rm ./RNS

create_symlinks:
	@echo Creating symlinks...
	-ln -s ../Reticulum/RNS ./
	-ln -s ../LXMF/LXMF ./

build_wheel:
	python3 setup.py sdist bdist_wheel

release: remove_symlinks build_wheel create_symlinks

upload:
	@echo Ready to publish release over Reticulum
	@read VOID
	rngit release rns://7649a50d84610232d1416b41d2896aff/reticulum/nomadnet create $$(python setup.py --getversion):dist --name nomadnet

upload-pip:
	@echo Uploading to PyPi...
	twine upload dist/*.whl dist/*.tar.gz
