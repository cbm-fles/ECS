#!/usr/bin/python3

from Statemachine import Statemachine
import csv
from _thread import start_new_thread
from multiprocessing import Queue
import zmq
from datetime import datetime
import threading
import struct # for packing integers
import logging
import ECSCodes
import configparser
import copy
import json
from DataObjects import DataObjectCollection, detectorDataObject, partitionDataObject
import sys
import Detector
from ECS_tools import MapWrapper

class PCA:
    id = None
    detectors = None
    stateMachine = None
    sequence = 0

    #---zmq sockets---
    #publish state updates
    socketPublish = None
    #pull updates from Detectors
    socketPullUpdates = None
    #update request from new Detectors or Detecotors with previous connection Problem
    socketServeCurrentStatus = None
    #request of status of an id
    socketSingleRequest = None
    #publish logmessages
    socketLogPublish = None
    poller = None

    logfile = None

    sem = None
    publishQueue = None
    commandQueue = None

    def __init__(self,id):
        self.id = id
        #read config File
        config = configparser.ConfigParser()
        config.read("init.cfg")
        conf = config["Default"]
        context = zmq.Context()

        #get your config
        configECS = None
        while configECS == None:
            requestSocket = context.socket(zmq.REQ)
            requestSocket.connect("tcp://%s:%s" % (conf['ECSAddress'],conf['ECSRequestPort']))
            requestSocket.setsockopt(zmq.RCVTIMEO, int(conf['receive_timeout']))
            requestSocket.setsockopt(zmq.LINGER,0)

            requestSocket.send_multipart([ECSCodes.pcaAsksForConfig, id.encode()])
            try:
                configJSON = requestSocket.recv().decode()
                configJSON = json.loads(configJSON)
                configECS = partitionDataObject(configJSON)
            except zmq.Again:
                print("timeout getting configuration")
                requestSocket.close()
                continue
            requestSocket.close()

        #init stuff
        self.detectors = {}
        self.activeDetectors = MapWrapper()
        self.sequence = 0
        #the only one who may change the status Map, is the publisher thread
        self.statusMap = MapWrapper()
        self.sem = threading.Semaphore()
        self.publishQueue = Queue()
        self.commandQueue = Queue()

        ports = config["ZMQPorts"]
        #ZMQ Socket to publish new state Updates
        self.socketPublish = context.socket(zmq.PUB)
        #todo the connect takes a little time messages until then will be lost
        self.socketPublish.bind("tcp://*:%s" % configECS.portPublish)

        #publish logmessages
        self.socketLogPublish = context.socket(zmq.PUB)
        self.socketLogPublish.bind("tcp://*:%s" % configECS.portLog)

        #Socket to wait for Updates From Detectors
        self.socketPullUpdates = context.socket(zmq.PULL)
        self.socketPullUpdates.bind("tcp://*:%s" % configECS.portUpdates)

        #Socket to serve current statusMap
        self.socketServeCurrentStatus = context.socket(zmq.ROUTER)
        self.socketServeCurrentStatus.bind("tcp://*:%s" % configECS.portCurrentState)

        #socket for receiving Status Updates
        self.socketSingleRequest = context.socket(zmq.REP)
        self.socketSingleRequest.bind("tcp://*:%s" % configECS.portSingleRequest)

        #socket for receiving commands
        self.remoteCommandSocket = context.socket(zmq.REP)
        self.remoteCommandSocket.bind("tcp://*:%s" % configECS.portCommand)

        #register Poller
        self.poller = zmq.Poller()
        #self.poller.register(self.socketPullUpdates, zmq.POLLIN)
        self.poller.register(self.socketServeCurrentStatus, zmq.POLLIN)
        self.poller.register(self.socketSingleRequest, zmq.POLLIN)

        #init logger
        self.logfile = conf["logPath"]
        debugMode = bool(conf["debugMode"])
        logging.basicConfig(
            format='%(asctime)s %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p',
            #level = logging.DEBUG,
            handlers=[
            #logging to file
            logging.FileHandler(self.logfile),
            #logging on console and WebUI
            logging.StreamHandler()
        ])
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger().handlers[0].setLevel(logging.INFO)
        #set console log to info level if in debug mode
        if debugMode:
            logging.getLogger().handlers[1].setLevel(logging.INFO)
        else:
            logging.getLogger().handlers[1].setLevel(logging.CRITICAL)

        #get your Detectorlist
        detList = None
        while detList == None:
            requestSocket = context.socket(zmq.REQ)
            requestSocket.connect("tcp://%s:%s" % (conf['ECSAddress'],conf['ECSRequestPort']))
            requestSocket.setsockopt(zmq.RCVTIMEO, int(conf['receive_timeout']))
            requestSocket.setsockopt(zmq.LINGER,0)

            requestSocket.send_multipart([ECSCodes.pcaAsksForDetectorList, self.id.encode()])
            try:
                #receive detectors as json
                ret = requestSocket.recv()
                if ret == ECSCodes.error:
                    self.log("received error getting DetectorList", True)
                    requestSocket.close()
                    continue
                detJSON = json.loads(ret.decode())
                #create DataObjectCollection from JSON
                detList = DataObjectCollection(detJSON,detectorDataObject)
            except zmq.Again:
                self.log("timeout getting DetectorList", True)
                requestSocket.close()
                continue
            requestSocket.close()

        start_new_thread(self.publisher,())


        def createDetectorThread(id,address,port, pingPort, type):
            #create the corresponding class for the specified type
            types = DetectorTypes()
            typeClass = types.getClassForType(type)
            confSection = types.getConfsectionForType(type)
            #todo all Detectors are added as active; in case of a crash the PCA needs to remember which Detectors were active; maybe save this information in the ECS database?
            det = typeClass(id,address,port,pingPort,confSection,self.log,self.publishQueue,self.handleDetectorTimeout,self.handleDetectorReconnect)
            self.addDetector(det)

        #create Detector objects
        threadArray = []
        for d in detList:
            #in case there of a connection problem, creating a Detector might take a long time, therefore create a own thread for each detector
            t = threading.Thread(name='dcreate'+str(d.id), target=self.addDetector, args=(d.id, d.address, d.port, d.pingPort, d.type))
            threadArray.append(t)
            t.start()
        for t in threadArray:
            t.join()
            #self.addDetector(det)

        #thread stuff
        start_new_thread(self.waitForUpdates,())
        start_new_thread(self.waitForRequests,())
        start_new_thread(self.waitForRemoteCommand,())
        start_new_thread(self.watchCommandQueue,())

        #set and publish PCA Globalstate
        #calculate global state
        globalState = "NotReady"
        allReady = True
        allRunning = True
        running = False
        for id in self.activeDetectors:
            d = self.detectors[id]
            if d.getMappedState() == "Running" and not running:
                running = True
            if d.getMappedState() != "Running":
                allRunning = False
            if d.getMappedState() != "Ready" and not running:
                allReady = False

        if running and not allRunning:
            globalState = "RunningInError"
        if allRunning:
            globalState = "Running"
        if not running and allReady:
            globalState = "Ready"

        self.stateMachine = Statemachine(conf["stateMachineCSV"],globalState)

        self.publishQueue.put((self.id,self.stateMachine.currentState))
        start_new_thread(self.checkCurrentState,())

    def watchCommandQueue(self):
        """watch the command Queue und execute incoming commands"""
        while True:
            m = self.commandQueue.get()
            #print(m)
            command,arg = m#self.commandQueue.get()
            if command==ECSCodes.getReady:
                self.makeReady()
                continue
            if command==ECSCodes.start:
                self.start()
                continue
            if command==ECSCodes.stop:
                self.stop()
                continue
            if command==ECSCodes.shutdown:
                self.shutdown()
                continue
            if command == ECSCodes.setActive:
                self.setDetectorActive(arg)
                continue
            if command == ECSCodes.setInactive:
                self.setDetectorInactive(arg)
                continue

    def waitForRemoteCommand(self):
        """wait for an external(start,stop etc.) command e.g. from that "gorgeous" WebGUI
        sends an Ok for received command"""
        while True:
            command = self.remoteCommandSocket.recv_multipart()
            arg = None
            if len(command) > 1:
                arg = command[1].decode()
            command = command[0]

            if command!=ECSCodes.ping:
                print (command)
            self.remoteCommandSocket.send(ECSCodes.ok)
            if command==ECSCodes.ping:
                #it's just a ping
                continue
            self.commandQueue.put((command,arg))


    def publisher(self):
        """publishes all state changes inside the Queue"""
        while True:
            id,state = self.publishQueue.get()
            #self.publishStateUpdate(id,state)
            #race condition possible?
            self.sequence = self.sequence + 1
            self.statusMap[id] = (self.sequence,state)
            print(id,self.sequence,state)
            self.send_status(self.socketPublish,id,self.sequence,state)


    def waitForUpdates(self):
        """wait for updates from Detectors"""
        while True:
            message = self.socketPullUpdates.recv()
            message = message.decode()
            id = None
            command = None
            print (message)

            if len(message.split()) != 2:
                self.log("received empty or too long message: %s" % message,True)
                continue

            id,command = message.split()

            if id == None or command == None:
                self.log("received non-valid message",True)
                continue
            if id not in self.detectors:
                self.log("received message with unknown id",True)
                continue

            #todo doesn't work anymore
            det = self.detectors[id]
            if (det.stateMachine.currentState == command):
                continue
            oldstate = det.stateMachine.currentState
            print(command)
            det.stateMachine.currentState = command
            #det.stateMachine.transition(command)
            self.log("Detector "+det.id+" had an unexpected Statechange "+ str(oldstate) +" -> " + det.stateMachine.currentState )
            self.publishQueue.put((det.id,det.getMappedState()))

    def waitForRequests(self):
        """waits for messages from detectors"""
        while True:
            try:
                items = dict(self.poller.poll())
            except:
                self.log("unexpected error while receiving messages")
                break

            #request for entire statusMap e.g. if a new Detector/Client connects
            if self.socketServeCurrentStatus in items:
                messsage = self.socketServeCurrentStatus.recv_multipart()
                origin = messsage[0]
                request = messsage[1]
                if request != ECSCodes.hello:
                    self.log("wrong request in socketServeCurrentStatus \n",True)
                    continue

                # send each Statusmap entry to origin
                items = self.statusMap.copy().items()
                for key, value in items:
                    #send identity of origin first
                    self.socketServeCurrentStatus.send(origin,zmq.SNDMORE)
                    #send key,sequence number,status
                    self.send_status(self.socketServeCurrentStatus,key,value[0],value[1])
                    #self.socketServeCurrentStatus.send_multipart([key,status[0],status[1]])

                # Final message
                self.socketServeCurrentStatus.send(origin, zmq.SNDMORE)
                self.send_status(self.socketServeCurrentStatus,None,self.sequence,None)

            #request for single Detector
            if self.socketSingleRequest in items:
                id = self.socketSingleRequest.recv().decode()

                if id in self.statusMap:
                    #send status
                    self.socketSingleRequest.send(self.statusMap.get[id][1].encode())
                else:
                    self.socketSingleRequest.send(ECSCodes.idUnknown)
                    self.log("received status request for unknown id",True)


    def send_status(self,socket,key,sequence,state):
        """method for sending a status update on a specified socket"""
        #if None send empty byte String
        key_b=b""
        if key != None:
            key_b = key.encode()

        #integers need to be packed
        sequence_s = struct.pack("!i",sequence)
        #python strings need to be encoded into binary strings
        state_b = b""
        if state != None:
            state_b = state.encode()
        socket.send_multipart([key_b,sequence_s,state_b])

    def addDetector(self, id, address, port, pingPort, type):
        """add Detector to Dictionary and pubish it's state"""
        #create the corresponding class for the specified type
        types = Detector.DetectorTypes()
        typeClass = types.getClassForType(type)
        confSection = types.getConfsectionForType(type)
        #todo all Detectors are added as active; in case of a crash the PCA needs to remember which Detectors were active; maybe save this information in the ECS database?
        det = typeClass(id,address,port,pingPort,confSection,self.log,self.publishQueue,self.handleDetectorTimeout,self.handleDetectorReconnect)
        self.detectors[det.id] = det
        if det.active:
            self.activeDetectors[det.id] = det.id
        self.publishQueue.put((det.id,det.getMappedState()))

    def removeDetector(self,id):
        """remove Detector from Dictionary"""
        #todo muss irgendwie richtig mitgeteilt werden
        det = self.detectors[id]
        #todo add possibility to cancel transitions?
        self.sem.acquire()
        det.shutdown()
        self.setDetectorInactive(id)
        self.publishQueue.put((d.id,"REMOVED"))
        del self.detectors[id]
        self.sem.release()

    def setDetectorActive(self,id):
        detector = self.detectors[id]
        detector.setActive()
        self.activeDetectors[id] = id
        self.publishQueue.put((id,detector.getMappedState()))
        self.transitionDetectorIntoGlobalState(id)


    def setDetectorInactive(self,id):
        detector = self.detectors[id]
        del self.activeDetectors[id]
        detector.powerOff()
        detector.setInactive()
        self.publishQueue.put((id,"inactive"))

    def handleDetectorTimeout(self,id):
        self.publishQueue.put((id,"Connection Problem"))

    def handleDetectorReconnect(self,id,state):
        self.publishQueue.put((id,state))
        #if detector is active try to transition him into globalState
        if id in self.activeDetectors:
            self.transitionDetectorIntoGlobalState(id)

    def transitionDetectorIntoGlobalState(self,id):
        """try to transition Detector to global State of the PCA"""
        det = self.detectors[id]
        if self.stateMachine.currentState == "NotReady":
            if det.getMappedState() == "Running":
                det.stop()
                return
        if self.stateMachine.currentState == "Ready":
            if det.getMappedState() == "NotReady":
                det.getReady()
                return
            if det.getMappedState() == "Running":
                det.stop()
                return
        if self.stateMachine.currentState == "Running" or self.stateMachine.currentState == "RunningInError":
            if det.getMappedState() == "NotReady":
                det.getReady()
                det.start()
                return
            if det.getMappedState() == "Ready":
                det.start()
                return


    def checkCurrentState(self):
        """checks in a loop if the current state is still valid, does necessary transition in case it is not"""
        while True:
            if self.activeDetectors.size() <= 0:
                continue
            #don't check your state while it's beeing changed that might induce chaos
            self.sem.acquire()
            if self.stateMachine.currentState == "NotReady":
                ready = True
                for id in self.activeDetectors:
                    d = self.detectors[id]
                    if d.getMappedState() != "Ready":
                        ready = False
                if ready:
                    self.transition("configured")

            if self.stateMachine.currentState == "Ready":
                for id in self.activeDetectors:
                    d = self.detectors[id]
            		#The PCA is not allowed to move into the Running State on his own
            		#only ready -> not ready is possible
                    if d.getMappedState() != "Ready":
                        # some Detectors are not ready anymore
                        self.error()


            if self.stateMachine.currentState == "Running":
                for id in self.activeDetectors:
                    d = self.detectors[id]
                #Some Detector stopped Working
                    if d.getMappedState() != "Running":
                        self.error()

            if self.stateMachine.currentState == "RunningInError":
                countDetectors = 0
                for id in self.activeDetectors:
                    d = self.detectors[id]
                    if d.getMappedState() == "Running":
                        countDetectors = countDetectors +1
                if countDetectors == self.activeDetectors.size():
                    #All Detecotors are working again
                    self.transition("resolved")
                if countDetectors == 0:
                    #All detectors are dead :(
                    self.transition("stop")
            self.sem.release()

    def transition(self,command):
        """try to transition the own Statemachine"""
        oldstate = self.stateMachine.currentState
        if self.stateMachine.transition(command):
            self.publishQueue.put((self.id,self.stateMachine.currentState))
            self.log("GLobal Statechange: "+oldstate+" -> "+self.stateMachine.currentState)

    def error(self):
        """make an error transition"""
        self.transition("error")

    def threadFunctionCall(self,id,function,retq):
        """calls a function from Detector(id) und puts result in a given Queue"""
        r = function()
        retq.put((id,r))

    def shutdown(self):
        """tells all Detectors to shutdown"""
        threadArray = []
        returnQueue = Queue()
        if not (len(self.detectors)==self.activeDetectors.size()):
            self.log("Warning: Some Detectors are disconnected")
        self.sem.acquire()
        for id in self.activeDetectors:
            d = self.detectors[id]
            t = threading.Thread(name='doff'+str(d.id), target=self.threadFunctionCall, args=(d.id, d.powerOff, returnQueue))
            threadArray.append(t)
            t.start()
        for t in threadArray:
            ret = returnQueue.get()
            d = self.detectors[ret[0]]
            if ret[1] != True:
                #todo something needs to happen here
                1+1
        self.sem.release()

    def makeReady(self):
        """tells all Detectors to get ready to start"""
        threadArray = []
        returnQueue = Queue()
        self.sem.acquire()
        for id in self.activeDetectors:
            d = self.detectors[id]
            t = threading.Thread(name='dready'+str(d.id), target=self.threadFunctionCall, args=(d.id, d.getReady, returnQueue))
            threadArray.append(t)
            t.start()

        for t in threadArray:
            ret = returnQueue.get()
            d = self.detectors[ret[0]]
            if ret[1] != True:
                #todo something needs to happen here
                1+1
        self.sem.release()

    def start(self):
        """tells all Detectors to start running"""
        if not (len(self.detectors)==self.activeDetectors.size()):
            self.log("Warning: Some Detectors are disconnected")
        self.sem.acquire()
        if self.stateMachine.currentState != "Ready" and self.stateMachine.currentState != "RunningInError":
            self.log("start not possible in current state")
            self.sem.release()
            return
        threadArray = []
        returnQueue = Queue()
        for id in self.activeDetectors:
            d = self.detectors[id]
            t = threading.Thread(name='dstart'+str(d.id), target=self.threadFunctionCall, args=(d.id, d.start, returnQueue))
            threadArray.append(t)
            t.start()

        for t in threadArray:
            ret = returnQueue.get()
            d = self.detectors[ret[0]]
            if ret[1] != True:
                #todo something needs to happen here
                1+1
        self.transition("start")
        self.sem.release()

    def stop(self):
        """tells all Detectors to stop running"""
        threadArray = []
        returnQueue = Queue()
        if not (len(self.detectors)==self.activeDetectors.size()):
            self.log("Warning: Some Detectors are disconnected")
        self.sem.acquire()
        for id in self.activeDetectors:
            d = self.detectors[id]
            t = threading.Thread(name='dstop'+str(d.id), target=self.threadFunctionCall, args=(d.id, d.stop, returnQueue))
            threadArray.append(t)
            t.start()

        for t in threadArray:
            ret = returnQueue.get()
            d = self.detectors[ret[0]]
            if ret[1] != True:
                #todo something needs to happen here
                1+1
        self.transition("stop")
        self.sem.release()

    def log(self,logmessage,error=False):
        str=datetime.now().strftime("%Y-%m-%d %H:%M:%S")+":" + logmessage
        self.socketLogPublish.send(str.encode())
        if error:
            logging.critical(logmessage)
        else:
            logging.info(logmessage)




if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("please enter the pca id")
        sys.exit(1)

    test = PCA(sys.argv[1])

    x = ""
    while x != "end":
        print ("1: get ready")
        print ("2: start")
        print ("3: stop")
        print ("4: shutdown")

        x = input()
        if x == "1":
            test.commandQueue.put((ECSCodes.getReady,None))
        if x == "2":
            test.commandQueue.put((ECSCodes.start,None))
        if x== "3":
            test.commandQueue.put((ECSCodes.stop,None))
        if x== "4":
            test.commandQueue.put((ECSCodes.shutdown,None))
