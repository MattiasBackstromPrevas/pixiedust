# -------------------------------------------------------------------------------
# Copyright IBM Corp. 2016
# 
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
# http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# -------------------------------------------------------------------------------
import os
import subprocess
import re
import inspect
from IPython.core.magic import (Magics, magics_class, cell_magic)
from pixiedust.utils.javaBridge import *
from pixiedust.utils.template import *
import pixiedust

myLogger = pixiedust.getLogger(__name__)

'''
Manages the variables defined interactively in the Notebook
'''
class InteractiveVariables(object):
    def __init__(self, shell):
        self.shell = shell

    def getVar(self, varName):
        return self.shell.user_ns.get(varName, None ) or self.shell.user_ns_hidden.get(varName, None)

    def transform(self, varName, varValue):
        pythonToScalaSimpleTypeMap = {"str":"String","int":"Int"}
        scalaType = pythonToScalaSimpleTypeMap.get(varValue.__class__.__name__, None) 
        codeValue = None
        initValue = None
        if scalaType is not None:
            primitive = True
            codeValue = "\"" + varValue.replace('\n','\\n') + "\"" if scalaType == "String" else varValue
        else:
            #Try the ConverterRegistry
            initValue,scalaType = ConverterRegistry.toJava(varValue)
        
        return {"value": varValue, "codeValue": codeValue, "type": scalaType or "Any", "initValue":initValue}

    def getVarsDict(self):
        user_ns = self.shell.user_ns
        user_ns_hidden = self.shell.user_ns_hidden
        nonmatching = object()  # This can never be in user_ns
        #for i in user_ns:
        #    print(user_ns[i].__class__)
        filtered = ["function"]
        out = { key : self.transform(key, user_ns[key]) for key in user_ns \
                if not key.startswith('_') \
                and key!="sc" and key!="sqlContext" \
                and (user_ns[key] is not user_ns_hidden.get(key, nonmatching)) \
                and not inspect.isclass(user_ns[key])\
                and not inspect.isfunction(user_ns[key])\
                and not inspect.ismodule(user_ns[key])}
        return out

    def updateVarsDict(self, vars):
        self.shell.user_ns.update(vars)

class ConverterRegistry(object):
    pythonToJavaConverters=[]

    @staticmethod
    def toJava(obj):
        for converter in ConverterRegistry.pythonToJavaConverters:
            v, t = converter(obj)
            if v is not None and t is not None:
                return (v,t)
        return (None,None)

def toJavaConverter(func):
    ConverterRegistry.pythonToJavaConverters.append( func )
    def wrapper(*args,**kwargs):
        return func(*args, **kwargs)
    return wrapper


@magics_class
class PixiedustScalaMagics(Magics):
    def __init__(self, shell):
        super(PixiedustScalaMagics,self).__init__(shell)
        self.interactiveVariables = InteractiveVariables(shell)
        self.scala_home = os.environ.get("SCALA_HOME")
        self.class_path = JavaWrapper("java.lang.System").getProperty("java.class.path")
        self.env = PixiedustTemplateEnvironment()

    def getLineOption(self, line, optionName):
        m=re.search(r"\b" + optionName + r"=(\S+)",line)
        return m.group(1) if m is not None else None

    def hasLineOption(self, line, optionName):
        return re.match(r"\b" + optionName + r"\b", line) is not None

    def getReturnVars(self, code):
        vars=set()
        for m in re.finditer(r"\b__(\w+?)\b",code):
            vars.add(m.group(0))
        return vars

    def fromJava(self, stuff):
        if stuff.__class__.__name__ == "JavaObject":
            if stuff.getClass().getName() == "org.apache.spark.sql.DataFrame":
                return DataFrame(stuff, SQLContext(SparkContext.getOrCreate(), stuff.sqlContext()))
            elif stuff.getClass().getName() == "org.apache.spark.sql.SQLContext":
                return SQLContext(SparkContext.getOrCreate(),stuff)
        return stuff

    @cell_magic
    def scala(self, line, cell):
        if not self.scala_home:
            print("Error Cannot run scala code: SCALA_HOME environment variable not set")
            return
        
        #generate the code
        scalaCode = self.env.getTemplate("scalaCell.template").render(
            cell=cell, variables=self.interactiveVariables.getVarsDict(), returnVars=self.getReturnVars(cell)
        )
        if self.hasLineOption(line, "debug"):
            print(scalaCode)
            return
        
        #build the scala object
        dir=os.path.expanduser('~') + "/pixiedust"
        if not os.path.exists(dir):
            os.makedirs(dir)
        source="pixiedustRunner.scala"
        with open(dir + "/" + source, "w") as f:
            f.write(scalaCode)
        #Compile the code
        proc = subprocess.Popen([self.scala_home + "/bin/scalac","-classpath", self.class_path, source],stdout=subprocess.PIPE,stderr=subprocess.PIPE, cwd=dir)
        code = proc.wait()
        if code != 0:
            while True:
                line = proc.stderr.readline()
                if not line:
                    break
                print(line.rstrip())
            return
        
        #Load the class and initialize the variables
        f = sc._jvm.java.io.File(dir)
        url = f.toURL()
        urls=sc._gateway.new_array(sc._jvm.java.net.URL,1)
        urls[0]=url

        cl = sc._jvm.java.net.URLClassLoader(urls)
        cls = sc._jvm.java.lang.Class.forName("com.ibm.pixiedust.PixiedustScalaRun$", True, cl)

        runnerObject = JavaWrapper(cls.getField("MODULE$").get(None), True, 
            self.getLineOption(line, "channel"), self.getLineOption(line, "receiver"))
        runnerObject.callMethod("init", pd_getJavaSparkContext(), self.interactiveVariables.getVar("sqlContext")._ssql_ctx )
        
        #Init the variables
        for key, val in self.interactiveVariables.getVarsDict().iteritems():
            if val["initValue"] is not None:
                runnerObject.callMethod("set" + key.capitalize(), val["initValue"])
        
        varMap = runnerObject.callMethod("runCell")

        #capture the return vars and update the interactive shell
        returnVars = {}
        it = varMap.iterator()
        while it.hasNext():
            t = it.next()
            returnVars[t._1()] = self.fromJava(t._2())
        self.interactiveVariables.updateVarsDict(returnVars)

        #discard the ClassLoader, we only use it within the context of a cell.
        #TODO: change that when we support inline scala class/object definition
        cl.close()
        cl=None
        cls=None
        runnerObject=None

try:
    get_ipython().register_magics(PixiedustScalaMagics)
except NameError:
    #IPython not available we must be in a spark executor
    pass

#converters
@toJavaConverter
def dataFrameConverter(var):
    from pyspark.sql import DataFrame
    if isinstance(var, DataFrame):
        return (var._jdf, "org.apache.spark.sql.DataFrame")
    return (None,None)

@toJavaConverter
def rddConverter(var):
    from pyspark.rdd import RDD
    if isinstance(var, RDD):
        return (var._jrdd, var._jrdd.getClass().getName() + "[Object]")
    return (None,None)

@toJavaConverter
def graphFrameConverter(var):
    if hasattr(var, "__module__") and hasattr(var, "__class__"):
        clazz = var.__module__ + "." + var.__class__.__name__
        if clazz == "graphframes.graphframe.GraphFrame":
            return (var._jvm_graph, "org.graphframes.GraphFrame")
    return (None,None)