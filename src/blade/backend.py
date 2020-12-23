# Copyright (c) 2011 Tencent Inc.
# All rights reserved.
#
# Author: Huan Yu <huanyu@tencent.com>
#         Feng Chen <phongchen@tencent.com>
#         Yi Wang <yiwang@tencent.com>
#         Chong Peng <michaelpeng@tencent.com>
# Date:   October 20, 2011


"""
 This is the build rules genearator module which invokes all the builder
 objects to generate build rules.
"""

from __future__ import absolute_import

import os
import subprocess
import sys
import textwrap

from blade import blade_util
from blade import config
from blade import console


def _incs_list_to_string(incs):
    """ Convert incs list to string
    ['thirdparty', 'include'] -> -I thirdparty -I include
    """
    return ' '.join(['-I ' + path for path in incs])


def protoc_import_path_option(incs):
    return ' '.join(['-I=%s' % inc for inc in incs])


def _shell_support_pipefail():
    """Whether current shell support the `pipefail` option"""
    return subprocess.call('set -o pipefail 2>/dev/null', shell=True) == 0


class _NinjaFileHeaderGenerator(object):
    """Generate global declarations and definitions for build script.

    Specifically it may consist of global functions and variables,
    environment setup, predefined rules and builders, utilities
    for the underlying build system.
    """
    # pylint: disable=too-many-public-methods
    def __init__(self, options, build_dir, blade_path, build_toolchain, blade):
        self.options = options
        self.build_dir = build_dir
        self.blade_path = blade_path
        self.build_toolchain = build_toolchain
        self.build_accelerator = blade.build_accelerator
        self.blade = blade

        self.rules_buf = []
        self.__all_rule_names = set()

    def _add_rule(self, rule):
        """Append one rule to buffer. """
        self.rules_buf.append('%s\n' % rule)

    def get_all_rule_names(self):
        return list(self.__all_rule_names)

    def generate_rule(self, name, command, description=None,
                      depfile=None, generator=False, pool=None,
                      restat=False, rspfile=None,
                      rspfile_content=None, deps=None):
        self.__all_rule_names.add(name)
        self._add_rule('rule %s' % name)
        self._add_rule('  command = %s' % command)
        if description:
            self._add_rule('  description = %s' % console.colored(description, 'dimpurple'))
        if depfile:
            self._add_rule('  depfile = %s' % depfile)
        if generator:
            self._add_rule('  generator = 1')
        if pool:
            self._add_rule('  pool = %s' % pool)
        if restat:
            self._add_rule('  restat = 1')
        if rspfile:
            self._add_rule('  rspfile = %s' % rspfile)
        if rspfile_content:
            self._add_rule('  rspfile_content = %s' % rspfile_content)
        if deps:
            self._add_rule('  deps = %s' % deps)
        self._add_rule('')  # An empty line to improve readability

    def generate_file_header(self):
        self._add_rule(textwrap.dedent('''\
                # build.ninja generated by blade
                ninja_required_version = 1.7
                builddir = %s
                ''') % self.build_dir)
        # No more than 1 heavy target at a time
        self._add_rule(textwrap.dedent('''\
                pool heavy_pool
                  depth = 1
                '''))

    def generate_common_rules(self):
        self.generate_rule(name='copy',
                           command='cp -f ${in} ${out}',
                           description='COPY ${in} ${out}')

    def _get_cc_flags(self):
        """Get the common c/c++ flags."""
        global_config = config.get_section('global_config')
        cc_config = config.get_section('cc_config')

        cppflags = []
        linkflags = []
        if self.options.m:
            cppflags = ['-m%s' % self.options.m]
            linkflags = ['-m%s' % self.options.m]
        # Add -fno-omit-frame-pointer to optimize mode for easy debugging.
        cppflags += ['-pipe', '-fno-omit-frame-pointer']

        # Debugging information setting
        debug_info_level = global_config['debug_info_level']
        debug_info_options = cc_config['debug_info_levels'][debug_info_level]
        cppflags += debug_info_options

        # Option debugging flags
        if self.options.profile == 'debug':
            cppflags.append('-fstack-protector')
        elif self.options.profile == 'release':
            cppflags.append('-DNDEBUG')

        cppflags += [
            '-D_FILE_OFFSET_BITS=64',
            '-D__STDC_CONSTANT_MACROS',
            '-D__STDC_FORMAT_MACROS',
            '-D__STDC_LIMIT_MACROS',
        ]

        if getattr(self.options, 'gprof', False):
            cppflags.append('-pg')
            linkflags.append('-pg')

        if getattr(self.options, 'coverage', False):
            cppflags.append('--coverage')
            linkflags.append('--coverage')

        cppflags = self.build_toolchain.filter_cc_flags(cppflags)
        return cppflags, linkflags

    def _get_warning_flags(self):
        """Get the warning flags. """
        cc_config = config.get_section('cc_config')
        cppflags = cc_config['warnings']
        cxxflags = cc_config['cxx_warnings']
        cflags = cc_config['c_warnings']

        filtered_cppflags = self.build_toolchain.filter_cc_flags(cppflags)
        filtered_cxxflags = self.build_toolchain.filter_cc_flags(cxxflags, 'c++')
        filtered_cflags = self.build_toolchain.filter_cc_flags(cflags, 'c')

        return filtered_cppflags, filtered_cxxflags, filtered_cflags

    def generate_cc_vars(self):
        warnings, cxx_warnings, c_warnings = self._get_warning_flags()
        c_warnings += warnings
        cxx_warnings += warnings
        # optimize_flags is need for `always_optimize`
        optimize_flags = config.get_item('cc_config', 'optimize')
        optimize = '$optimize_flags' if self.options.profile == 'release' else ''
        self._add_rule(textwrap.dedent('''\
                c_warnings = %s
                cxx_warnings = %s
                optimize_flags = %s
                optimize = %s
                ''') % (' '.join(c_warnings), ' '.join(cxx_warnings),
                        ' '.join(optimize_flags), optimize))

    def _hdrs_command(self, cc, flags, cppflags, includes):
        """Command to generate cc inclusion information file"""
        args = '-o /dev/null -E -H %s %s -w ${cppflags} %s ${includes} ${in} 2> ${out}' % (
                ' '.join(flags), ' '.join(cppflags), includes)
        # The `-fdirectives-only` option can significantly increase the speed of preprocessing,
        # but errors may occur under certain boundary conditions (for example, check `__COUNTER__`
        # in the preprocessing directives), rerun the command without it on error.
        preprocess1 = '%s -fdirectives-only %s' % (cc, args)
        preprocess2 = '%s %s' % (cc, args)
        return preprocess1 + ' || ' + preprocess2

    def generate_cc_rules(self):
        # pylint: disable=too-many-locals
        cc, cxx, ld = self.build_accelerator.get_cc_commands()
        cc_config = config.get_section('cc_config')
        cc_library_config = config.get_section('cc_library_config')
        cflags, cxxflags = cc_config['cflags'], cc_config['cxxflags']
        cppflags, ldflags = self._get_cc_flags()
        cppflags = cc_config['cppflags'] + cppflags
        arflags = ''.join(cc_library_config['arflags'])
        ldflags = cc_config['linkflags'] + ldflags
        includes = cc_config['extra_incs']
        includes = includes + ['.', self.build_dir]
        includes = ' '.join(['-I%s' % inc for inc in includes])

        self.generate_cc_vars()

        # To verify whether a header file is included without depends on the library it belongs to,
        # we use the gcc's `-H` option to generate the inclusion stack information, see
        # https://gcc.gnu.org/onlinedocs/gcc/Preprocessor-Options.html for details.
        # But this information is output to stderr mixed with diagnostic messages.
        # So we use this awk script to split them.
        #
        # NOTE the `$$` is required by ninja. and the useless `Multiple ...` is the last part of
        # the messages.
        awk_script = ("""'BEGIN {stop=0} /^Multiple include guards may be useful for:/ {stop=1}"""
                      """ !stop {if ($$1 ~/^\.+$$/) print $$0; else print $$0 > "/dev/stderr"}'""")

        if _shell_support_pipefail():
            # Use `pipefail` to ensure that the exit code is correct.
            template = 'set -o pipefail && %%s -H 2>&1 | awk %s > ${out}.H' % awk_script
        else:
            # Some shell such as `dash` under Ubuntu doesn't support pipefail, make a workaround.
            template = '%%s -H 2> ${out}.err && awk %s < ${out}.err > ${out}.H && rm -f ${out}.err' % awk_script

        cc_command = ('%s -o ${out} -MMD -MF ${out}.d -c -fPIC %s %s ${optimize} '
                      '${c_warnings} ${cppflags} %s ${includes} ${in}') % (
                              cc, ' '.join(cflags), ' '.join(cppflags), includes)
        self.generate_rule(name='cc',
                           command=template % cc_command,
                           description='CC ${in}',
                           depfile='${out}.d',
                           deps='gcc')

        cxx_command = ('%s -o ${out} -MMD -MF ${out}.d -c -fPIC %s %s ${optimize} '
                       '${cxx_warnings} ${cppflags} %s ${includes} ${in}') % (
                               cxx, ' '.join(cxxflags), ' '.join(cppflags), includes)
        self.generate_rule(name='cxx',
                           command=template % cxx_command,
                           description='CXX ${in}',
                           depfile='${out}.d',
                           deps='gcc')

        securecc = '%s %s' % (cc_config['securecc'], cxx)
        self.generate_rule(name='securecccompile',
                           command='%s -o ${out} -c -fPIC '
                                   '%s %s ${optimize} ${cxx_warnings} ${cppflags} %s ${includes} ${in}' % (
                                       securecc, ' '.join(cxxflags), ' '.join(cppflags), includes),
                           description='SECURECC ${in}')
        self.generate_rule(name='securecc',
                           command=self._builtin_command('securecc_object'),
                           description='SECURECC ${in}',
                           restat=True)

        self.generate_rule(name='ar',
                           command='rm -f $out; ar %s $out $in' % arflags,
                           description='AR ${out}')
        link_jobs = config.get_item('link_config', 'link_jobs')
        if link_jobs:
            link_jobs = min(link_jobs, self.blade.build_jobs_num())
            console.info('Adjust parallel link jobs number to %s' % link_jobs)
            pool = 'link_pool'
            self._add_rule(textwrap.dedent('''\
                    pool %s
                      depth = %s''') % (pool, link_jobs))
        else:
            pool = None
        self.generate_rule(name='link',
                           command='%s -o ${out} %s ${ldflags} ${in} ${extra_ldflags}' % (
                               ld, ' '.join(ldflags)),
                           description='LINK ${out}',
                           pool=pool)
        self.generate_rule(name='solink',
                           command='%s -o ${out} -shared %s ${ldflags} ${in} ${extra_ldflags}' % (
                               ld, ' '.join(ldflags)),
                           description='SHAREDLINK ${out}',
                           pool=pool)
        self.generate_rule(name='strip',
                           command='strip --strip-unneeded -o ${out} ${in}',
                           description='STRIP ${out}')

    def generate_proto_rules(self):
        proto_config = config.get_section('proto_library_config')
        protoc = proto_config['protoc']
        protoc_java = protoc
        if proto_config['protoc_java']:
            protoc_java = proto_config['protoc_java']
        protobuf_incs = protoc_import_path_option(proto_config['protobuf_incs'])
        protobuf_java_incs = protobuf_incs
        if proto_config['protobuf_java_incs']:
            protobuf_java_incs = protoc_import_path_option(proto_config['protobuf_java_incs'])
        self._add_rule(textwrap.dedent('''\
                protocflags =
                protoccpppluginflags =
                protocjavapluginflags =
                protocpythonpluginflags =
                '''))
        self.generate_rule(name='proto',
                           command='%s --proto_path=. %s -I=`dirname ${in}` '
                                   '--cpp_out=%s ${protocflags} ${protoccpppluginflags} ${in}' % (
                                       protoc, protobuf_incs, self.build_dir),
                           description='PROTOC ${in}')
        self.generate_rule(name='protojava',
                           command='%s --proto_path=. %s --java_out=%s/`dirname ${in}` '
                                   '${protocjavapluginflags} ${in}' % (
                                       protoc_java, protobuf_java_incs, self.build_dir),
                           description='PROTOCJAVA ${in}')
        self.generate_rule(name='protopython',
                           command='%s --proto_path=. %s -I=`dirname ${in}` '
                                   '--python_out=%s ${protocpythonpluginflags} ${in}' % (
                                       protoc, protobuf_incs, self.build_dir),
                           description='PROTOCPYTHON ${in}')
        self.generate_rule(name='protodescriptors',
                           command='%s --proto_path=. %s -I=`dirname ${first}` '
                                   '--descriptor_set_out=${out} --include_imports '
                                   '--include_source_info ${in}' % (
                                       protoc, protobuf_incs),
                           description='PROTODESCRIPTORS ${in}')
        protoc_go_plugin = proto_config['protoc_go_plugin']
        if protoc_go_plugin:
            go_home = config.get_item('go_config', 'go_home')
            go_module_enabled = config.get_item('go_config', 'go_module_enabled')
            go_module_relpath = config.get_item('go_config', 'go_module_relpath')
            if not go_home:
                console.fatal('"go_config.go_home" is not configured')
            if go_module_enabled and not go_module_relpath:
                outdir = proto_config['protobuf_go_path']
            else:
                outdir = os.path.join(go_home, 'src')
            subplugins = proto_config['protoc_go_subplugins']
            if subplugins:
                go_out = 'plugins=%s:%s' % ('+'.join(subplugins), outdir)
            else:
                go_out = outdir
            self.generate_rule(name='protogo',
                               command='%s --proto_path=. %s -I=`dirname ${in}` '
                                       '--plugin=protoc-gen-go=%s --go_out=%s ${in}' % (
                                           protoc, protobuf_incs, protoc_go_plugin, go_out),
                               description='PROTOCGOLANG ${in}')

    def generate_resource_rules(self):
        args = '${name} ${path} ${out} ${in}'
        self.generate_rule(name='resource_index',
                           command=self._builtin_command('resource_index', suffix=args),
                           description='RESOURCE INDEX ${out}')
        self.generate_rule(name='resource',
                           command='xxd -i ${in} | '
                                   'sed -e "s/^unsigned char /const char RESOURCE_/g" '
                                   '-e "s/^unsigned int /const unsigned int RESOURCE_/g" > ${out}',
                           description='RESOURCE ${in}')

    def get_java_command(self, java_config, cmd):
        java_home = java_config['java_home']
        if java_home:
            return os.path.join(java_home, 'bin', cmd)
        return cmd

    def get_jacocoagent(self):
        jacoco_home = config.get_item('java_test_config', 'jacoco_home')
        if jacoco_home:
            return os.path.join(jacoco_home, 'lib', 'jacocoagent.jar')
        return ''

    def generate_javac_rules(self, java_config):
        javac = self.get_java_command(java_config, 'javac')
        jar = self.get_java_command(java_config, 'jar')
        cmd = [javac]
        version = java_config['version']
        source_version = java_config.get('source_version', version)
        target_version = java_config.get('target_version', version)
        if source_version:
            cmd.append('-source %s' % source_version)
        if target_version:
            cmd.append('-target %s' % target_version)
        cmd += [
            '-encoding ${source_encoding}',
            '-d ${classes_dir}',
            '-classpath ${classpath}',
            '${javacflags}',
            '${in}',
        ]
        self._add_rule(textwrap.dedent('''\
                source_encoding = UTF-8
                classpath = .
                javacflags =
                '''))
        self.generate_rule(name='javac',
                           command='rm -fr ${classes_dir} && mkdir -p ${classes_dir} && '
                                   '%s && sleep 0.01 && '
                                   '%s cf ${out} -C ${classes_dir} .' % (
                                       ' '.join(cmd), jar),
                           description='JAVAC ${out}')

    def generate_java_resource_rules(self):
        self.generate_rule(name='javaresource',
                           command=self._builtin_command('java_resource'),
                           description='JAVA RESOURCE ${in}')

    def generate_java_test_rules(self):
        jacocoagent = self.get_jacocoagent()
        args = ('--script=${out} --main_class=${mainclass} --jacocoagent=%s '
                '--packages_under_test=${packages_under_test} ${in}') % jacocoagent
        self.generate_rule(name='javatest',
                           command=self._builtin_command('java_test', suffix=args),
                           description='JAVA TEST ${out}')

    def generate_java_binary_rules(self):
        bootjar = config.get_item('java_binary_config', 'one_jar_boot_jar')
        args = '--onejar=${out} --bootjar=%s --main_class=${mainclass} ${in}' % bootjar
        self.generate_rule(name='onejar',
                           command=self._builtin_command('java_onejar', suffix=args),
                           description='ONE JAR ${out}')
        self.generate_rule(name='javabinary',
                           command=self._builtin_command('java_binary'),
                           description='JAVA BIN ${out}')

    def generate_scalac_rule(self, java_config):
        scalac = 'scalac'
        scala_home = config.get_item('scala_config', 'scala_home')
        if scala_home:
            scalac = os.path.join(scala_home, 'bin', scalac)
        java = self.get_java_command(java_config, 'java')
        self._add_rule(textwrap.dedent('''\
                scalacflags = -nowarn
                '''))
        cmd = [
            'JAVACMD=%s' % java,
            scalac,
            '-encoding UTF8',
            '-d ${out}',
            '-classpath ${classpath}',
            '${scalacflags}',
            '${in}'
        ]
        self.generate_rule(name='scalac',
                           command=' '.join(cmd),
                           description='SCALAC ${out}')

    def generate_scalatest_rule(self, java_config):
        java = self.get_java_command(java_config, 'java')
        scala = 'scala'
        scala_home = config.get_item('scala_config', 'scala_home')
        if scala_home:
            scala = os.path.join(scala_home, 'bin', scala)
        jacocoagent = self.get_jacocoagent()
        args = ('--java=%s --scala=%s --jacocoagent=%s --packages_under_test=${packages_under_test} '
                '--script=${out} ${in}') % (java, scala, jacocoagent)
        self.generate_rule(name='scalatest', command=self._builtin_command('scala_test',
                                suffix=args),
                           description='SCALA TEST ${out}')

    def generate_java_scala_rules(self):
        java_config = config.get_section('java_config')
        self.generate_javac_rules(java_config)
        self.generate_java_resource_rules()
        jar = self.get_java_command(java_config, 'jar')
        args = '%s ${out} ${in}' % jar
        self.generate_rule(name='javajar',
                           command=self._builtin_command('java_jar', suffix=args),
                           description='JAVA JAR ${out}')
        self.generate_java_test_rules()
        self.generate_rule(name='fatjar',
                           command=self._builtin_command('java_fatjar'),
                           description='FAT JAR ${out}')
        self.generate_java_binary_rules()
        self.generate_scalac_rule(java_config)
        self.generate_scalatest_rule(java_config)

    def generate_thrift_rules(self):
        thrift_config = config.get_section('thrift_config')
        incs = _incs_list_to_string(thrift_config['thrift_incs'])
        gen_params = thrift_config['thrift_gen_params']
        thrift = thrift_config['thrift']
        if thrift.startswith('//'):
            thrift = thrift.replace('//', self.build_dir + '/')
            thrift = thrift.replace(':', '/')
        self.generate_rule(name='thrift',
                           command='%s --gen %s '
                                   '-I . %s -I `dirname ${in}` '
                                   '-out %s/`dirname ${in}` ${in}' % (
                                       thrift, gen_params, incs, self.build_dir),
                           description='THRIFT ${in}')

    def generate_python_rules(self):
        args = '--basedir=${basedir} --pylib=${out} ${in}'
        self.generate_rule(name='pythonlibrary',
                           command=self._builtin_command('python_library', suffix=args),
                           description='PYTHON LIBRARY ${out}')
        args = ('--basedir=${basedir} --exclusions=${exclusions} --mainentry=${mainentry} '
                '--pybin=${out} ${in}')
        self.generate_rule(name='pythonbinary',
                           command=self._builtin_command('python_binary', suffix=args),
                           description='PYTHON BINARY ${out}')

    def generate_go_rules(self):
        go_home = config.get_item('go_config', 'go_home')
        go = config.get_item('go_config', 'go')
        go_module_enabled = config.get_item('go_config', 'go_module_enabled')
        go_module_relpath = config.get_item('go_config', 'go_module_relpath')
        if go_home and go:
            go_pool = 'golang_pool'
            self._add_rule(textwrap.dedent('''\
                    pool %s
                      depth = 1
                    ''') % go_pool)
            go_path = os.path.normpath(os.path.abspath(go_home))
            out_relative = ""
            if go_module_enabled:
                prefix = go
                if go_module_relpath:
                    relative_prefix = os.path.relpath(prefix, go_module_relpath)
                    prefix = "cd {go_module_relpath} && {relative_prefix}".format(
                        go_module_relpath=go_module_relpath,
                        relative_prefix=relative_prefix,
                    )
                    # add slash to the end of the relpath
                    out_relative = os.path.join(os.path.relpath("./", go_module_relpath), "")
            else:
                prefix = 'GOPATH=%s %s' % (go_path, go)
            self.generate_rule(name='gopackage',
                               command='%s install ${extra_goflags} ${package}' % prefix,
                               description='GO INSTALL ${package}',
                               pool=go_pool)
            self.generate_rule(name='gocommand',
                               command='%s build -o %s${out} ${extra_goflags} ${package}' % (prefix, out_relative),
                               description='GO BUILD ${package}',
                               pool=go_pool)
            self.generate_rule(name='gotest',
                               command='%s test -c -o %s${out} ${extra_goflags} ${package}' % (prefix, out_relative),
                               description='GO TEST ${package}',
                               pool=go_pool)

    def generate_shell_rules(self):
        self.generate_rule(name='shelltest',
                           command=self._builtin_command('shell_test'),
                           description='SHELL TEST ${out}')
        args = '${out} ${in} ${testdata}'
        self.generate_rule(name='shelltestdata',
                           command=self._builtin_command('shell_testdata', suffix=args),
                           description='SHELL TEST DATA ${out}')

    def generate_lex_yacc_rules(self):
        self.generate_rule(name='lex',
                           command='flex ${lexflags} -o ${out} ${in}',
                           description='LEX ${in}')
        self.generate_rule(name='yacc',
                           command='bison ${yaccflags} -o ${out} ${in}',
                           description='YACC ${in}')

    def generate_package_rules(self):
        args = '${out} ${in} ${entries}'
        self.generate_rule(name='package',
                           command=self._builtin_command('package', suffix=args),
                           description='PACKAGE ${out}')
        self.generate_rule(name='package_tar',
                           command='tar -c -f ${out} ${tarflags} -C ${packageroot} ${entries}',
                           description='TAR ${out}')
        self.generate_rule(name='package_zip',
                           command='cd ${packageroot} && zip -q temp_archive.zip ${entries} && '
                                   'cd - && mv ${packageroot}/temp_archive.zip ${out}',
                           description='ZIP ${out}')

    def generate_version_rules(self):
        cc = self.build_toolchain.get_cc()
        cc_version = self.build_toolchain.get_cc_version()

        revision, url = blade_util.load_scm(self.build_dir)
        args = '--scm=${out} --revision=${revision} --url=${url} --profile=${profile} --compiler="${compiler}"'
        self.generate_rule(name='scm',
                           command=self._builtin_command('scm', suffix=args),
                           description='SCM ${out}')
        scm = os.path.join(self.build_dir, 'scm.cc')
        self._add_rule(textwrap.dedent('''\
                build %s: scm
                  revision = %s
                  url = %s
                  profile = %s
                  compiler = %s
                ''') % (scm, revision, url, self.options.profile, '%s %s' % (cc, cc_version)))
        self._add_rule(textwrap.dedent('''\
                build %s: cxx %s
                  cppflags = -w -O2
                  cxx_warnings =
                ''') % (scm + '.o', scm))

    def _builtin_command(self, builder, prefix='', suffix=''):
        cmd = ['PYTHONPATH=%s:$$PYTHONPATH' % self.blade_path]
        if prefix:
            cmd.append(prefix)
        cmd.append('%s -m blade.builtin_tools %s' % (sys.executable, builder))
        if suffix:
            cmd.append(suffix)
        else:
            cmd.append('${out} ${in}')
        return ' '.join(cmd)

    def generate(self):
        """Generate ninja rules. """
        self.generate_file_header()
        self.generate_common_rules()
        self.generate_cc_rules()
        self.generate_proto_rules()
        self.generate_resource_rules()
        self.generate_java_scala_rules()
        self.generate_thrift_rules()
        self.generate_python_rules()
        self.generate_go_rules()
        self.generate_shell_rules()
        self.generate_lex_yacc_rules()
        self.generate_package_rules()
        self.generate_version_rules()
        return self.rules_buf


class NinjaFileGenerator(object):
    """Generate ninja rules to build.ninja. """

    def __init__(self, ninja_path, blade_path, blade):
        self.script_path = ninja_path
        self.blade_path = blade_path
        self.blade = blade
        self.build_toolchain = blade.get_build_toolchain()
        self.build_dir = blade.get_build_dir()
        self.__all_rule_names = []

    def get_all_rule_names(self):
        return self.__all_rule_names

    def generate_build_rules(self):
        """Generate ninja rules to build.ninja. """
        ninja_script_header_generator = _NinjaFileHeaderGenerator(
            self.blade.get_options(),
            self.build_dir,
            self.blade_path,
            self.build_toolchain,
            self.blade)
        rules = ninja_script_header_generator.generate()
        rules += self.blade.gen_targets_rules()
        self.__all_rule_names = ninja_script_header_generator.get_all_rule_names()
        return rules

    def generate_build_script(self):
        """Generate build script for underlying build system. """
        rules = self.generate_build_rules()
        script = open(self.script_path, 'w')
        script.writelines(rules)
        script.close()
        return rules
